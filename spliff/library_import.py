"""Plan and locally approve filing a first-time download without overwriting files."""
from contextlib import closing, contextmanager
import json
from pathlib import Path
import os
import re
import shutil
import sys

from . import discogs, flac_metadata, keep_both
from .artist import artist_key
from .catalog import connect, file_identity, file_metadata_mode, find_probe, read_audio, signature
from .nicotine_bridge.downloads import selection_parts
from .nicotine_bridge.wire import staging_root
from .scanner import AUDIO_FIELDS
from .staged_downloads import edition_track_matches, review_download, title_key, track_position

CATEGORIES = ("Albums", "Posthumous Albums", "EPs", "Mixtapes", "Singles", "Tracks")


def music_text(value):
    text = value.strip() if isinstance(value, str) else ""
    if text.isprintable() and len(text.encode("utf-8")) <= 180 and not re.search(r"https?://|www\.", text, re.I):
        return text
    return ""


def folder_name(value):
    name = re.sub(r'[/\\:*?"<>|]', " – ", value).strip(" .")
    if not name or len(name.encode("utf-8")) > 220:
        raise ValueError("Choose a shorter artist, release, or track name.")
    return name


def ordered_items(items):
    def number(value, fallback):
        value = str(value or "").split("/")[0]
        return int(value) if value.isascii() and value.isdigit() else fallback

    return [item for _, item in sorted(enumerate(items, 1), key=lambda entry: (
        number(entry[1]["audio"].get("disc_number"), 1),
        track_position(entry[1]["audio"], entry[1]["name"]) or entry[0], entry[0]))]


def source_report(db, candidate):
    selection_parts(candidate)
    report = review_download(db, candidate, include_sources=True)
    if report["kept_both"]:
        raise ValueError("This download is already in your library.")
    if not report["items"] or report["complete"] != len(report["items"]):
        raise ValueError("Wait for every file in this transfer to finish downloading.")
    if any(item.get("audio", {}).get("quality") != "lossless" or not item.get("source")
           for item in report["items"]):
        raise ValueError("Every file needs to pass lossless audio inspection before filing this transfer.")
    report["items"] = ordered_items(report["items"])
    return report


def seed(report):
    items = [item["audio"] for item in report["items"]]
    artists = {music_text(item.get("album_artist") or item.get("artist")) for item in items}
    releases = {music_text(item.get("album")) for item in items}
    years = {re.match(r"^([12]\d{3})(?:$|-)", str(item.get("date") or "")) for item in items}
    years = {match[1] for match in years if match}
    return {"artist": next(iter(artists)) if len(artists) == 1 else "",
            "title": (next(iter(releases)) if len(releases) == 1 else "") or
                     (music_text(items[0].get("title")) if report["kind"] == "track" else ""),
            "year": next(iter(years)) if len(years) == 1 else "",
            "category": "Tracks" if report["kind"] == "track" and releases == {""} else ""}


def match_edition(report, edition):
    guess = seed(report)
    artist_names = [name for name in edition.get("artists", []) if music_text(name)]
    variations = [credit.get("anv") for credit in edition.get("artist_credits", []) if music_text(credit.get("anv"))]
    names = artist_names + variations
    listing = [track for track in edition.get("tracklist", []) if track.get("type_") == "track"]
    if (not artist_names or not guess["artist"] or not guess["title"]
            or title_key(edition.get("title")) != title_key(guess["title"])
            or title_key(guess["artist"]) not in {title_key(name) for name in names}
            or len(listing) != len(report["items"])
            or not all(edition_track_matches(track, item["audio"]) for track, item in zip(listing, report["items"]))):
        return None
    descriptions = {str(description).casefold() for fmt in edition.get("formats") or []
                    for description in fmt.get("descriptions") or []}
    categories = [category for label, category in (("album", "Albums"), ("ep", "EPs"),
                  ("mixtape", "Mixtapes"), ("single", "Singles")) if label in descriptions]
    aliases = []
    if len(artist_names) == 1 and edition.get("artist_credits"):
        try:
            aliases = discogs.artist_aliases(edition["artist_credits"][0].get("id"))
        except (OSError, ValueError):
            pass
    labels = [{key: music_text(label.get(key)) for key in ("name", "catno")}
              for label in edition.get("labels", [])]
    return {"id": edition["id"], "artist": ", ".join(artist_names), "title": music_text(edition["title"]),
            "year": str(edition["year"]) if edition.get("year") else "",
            "category": categories[0] if len(categories) == 1 else "",
            "country": music_text(edition.get("country")), "labels": labels,
            "credits": [{key: music_text(credit.get(key)) for key in ("name", "role")}
                        for credit in edition.get("credits", [])],
            "names": [name for name in (names + aliases if len(artist_names) == 1 else []) if music_text(name)],
            "tracks": [{key: music_text(track.get(key)) for key in ("title", "position", "duration")}
                       for track in listing]}


def options(db, candidate):
    report = source_report(db, candidate)
    suggested = seed(report)
    editions = []
    if suggested["artist"] and suggested["title"]:
        try:
            offers = discogs.search_releases(db, suggested["artist"], suggested["title"], limit=3)["items"]
        except (OSError, ValueError):
            offers = []
        for offer in offers:
            try:
                match = match_edition(report, discogs.release_details(db, offer["id"]))
            except (OSError, ValueError):
                continue
            if match:
                editions.append(match)
    with closing(connect(db)) as connection:
        roots = [row["path"] for row in connection.execute("SELECT path FROM roots ORDER BY path")]
    return {"roots": roots, "suggested": suggested, "editions": editions,
            "categories": CATEGORIES if report["kind"] == "track" else CATEGORIES[:-1]}


def check_root(db, root, identity):
    if not root.is_absolute() or root == Path(root.anchor) or db.resolve().is_relative_to(root):
        raise ValueError("Choose a music library folder outside Spliff’s catalog.")
    with keep_both.open_directory(root, {str(root): identity}):
        pass


def root_identity(db, folder):
    root = Path(folder).expanduser().resolve(strict=True)
    with keep_both.open_directory(root, {}) as descriptor:
        identity = file_identity(os.fstat(descriptor))
    check_root(db, root, identity)
    return str(root), identity


def catalog_roots(connection):
    return {row["path"]: (row["device"], row["inode"]) for row in
            connection.execute("SELECT path,device,inode FROM roots")}


def check_catalog_root(root, identity, roots):
    children = []
    for path, recorded in roots.items():
        existing = Path(path)
        if root == existing:
            if recorded != identity:
                raise ValueError("The library drive changed. Select the library again.")
        elif root.is_relative_to(existing):
            raise ValueError("Choose the existing indexed library that contains this folder.")
        elif existing.is_relative_to(root):
            with keep_both.open_directory(existing, {path: recorded}):
                children.append(existing)
    return children


def child_names(path, identities):
    with keep_both.open_directory(path, identities) as directory:
        return {artist_key(name): name for name in os.listdir(directory)}


@contextmanager
def read_pinned(path, expected, identities):
    with keep_both.open_directory(path.parent, identities) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, "rb") as source:
            if signature(os.fstat(descriptor)) != expected or keep_both.file_signature(parent, path.name) != expected:
                raise ValueError("A reviewed file changed.")
            yield source
            if signature(os.fstat(descriptor)) != expected or keep_both.file_signature(parent, path.name) != expected:
                raise ValueError("A reviewed file changed.")


def disc_number(item, edition_track):
    position = edition_track.get("position", "")
    disc = re.fullmatch(r"(?:CD)?([0-9]+)[.-][0-9]+", position, re.I)
    if disc:
        return max(1, int(disc[1]))
    value = str(item["audio"].get("disc_number") or "").split("/")[0]
    return max(1, int(value)) if re.fullmatch(r"[0-9]{1,3}", value) else 1


def prepare(db, candidate, root, identity, details, edition=None):
    if sys.platform != "darwin":
        raise ValueError("Adding downloads to your library currently requires macOS.")
    root = Path(root)
    check_root(db, root, identity)
    report = source_report(db, candidate)
    artist, title = (music_text(details.get(key)) for key in ("artist", "title"))
    year, category = details.get("year"), details.get("category")
    if not artist or not title or not isinstance(year, str) or not re.fullmatch(r"[12][0-9]{3}", year) or category not in CATEGORIES:
        raise ValueError("Confirm the artist, release title, four-digit edition year, and category.")
    if category == "Tracks" and report["kind"] != "track":
        raise ValueError("Choose a release category for this album.")
    posthumous = details.get("posthumous") is True
    if category == "Posthumous Albums" and not posthumous:
        raise ValueError("Confirm the album was first released after the artist’s death.")
    details = {"artist": artist, "title": title, "year": year, "category": category, "posthumous": posthumous}
    if edition and (title_key(artist) not in {title_key(name) for name in [edition["artist"], *edition["names"]]}
                    or title_key(title) != title_key(edition["title"]) or (edition["year"] and year != edition["year"])):
        raise ValueError("Clear the Discogs edition before changing its artist, title, or year.")
    with closing(connect(db)) as connection:
        check_catalog_root(root, identity, catalog_roots(connection))
        metadata_mode = file_metadata_mode(connection)
    job = staging_root() / candidate
    if job.is_relative_to(root) or root.is_relative_to(job):
        raise ValueError("Keep Spliff staging outside the selected music library.")

    identities = {str(root): identity}
    children = child_names(root, identities)
    canonical = artist_key(folder_name(artist))
    alternatives = {children[key] for name in (edition or {}).get("names", [])
                    if (key := artist_key(folder_name(name))) in children}
    artist_folder = children.get(canonical)
    if artist_folder is None and len(alternatives) > 1:
        raise ValueError("Several artist folders match. Enter the exact existing artist folder name to choose it.")
    artist_folder = artist_folder or next(iter(alternatives), folder_name(artist))
    artist_path = root / artist_folder
    new_folders = []
    if artist_key(artist_folder) in children:
        keep_both.remember_directory(artist_path, identities)
        categories = child_names(artist_path, identities)
    else:
        new_folders.append(str(artist_path))
        categories = {}
    category_path = artist_path / categories.get(artist_key(category), category)
    if artist_key(category) in categories:
        keep_both.remember_directory(category_path, identities)
        releases = child_names(category_path, identities)
    else:
        new_folders.append(str(category_path))
        releases = {}

    destination = category_path / f"[{year}] {folder_name(title)}"
    if artist_key(destination.name) in releases:
        raise ValueError("That release folder already exists. Choose a distinct destination before importing.")
    work = root / f".spliff-import-{candidate}"
    if artist_key(work.name) in children:
        raise ValueError(f"Review the interrupted import in {work} before retrying.")
    new_folders.append(str(destination))
    keep_both.remember_directory(job, identities)
    edition_tracks = (edition or {}).get("tracks") or [{} for _ in report["items"]]
    if len(edition_tracks) != len(report["items"]):
        raise ValueError("The download no longer matches the reviewed edition.")
    discs = [disc_number(item, track) for item, track in zip(report["items"], edition_tracks)]
    multi = len(set(discs)) > 1 or any(disc > 1 for disc in discs)
    if multi:
        new_folders.extend(str(destination / f"Disc {disc}") for disc in sorted(set(discs)))
    tracks = {}
    pairs = []
    for item, edition_track, disc in zip(report["items"], edition_tracks, discs):
        source = Path(item["source"])
        if source.parent.parent != job or ".." in source.parts or not re.fullmatch(r"[0-9]{3}", source.parent.name):
            raise ValueError("A download is outside its reviewed staging folder.")
        keep_both.remember_directory(source.parent, identities)
        position = tracks.get(disc, 0) + 1
        tracks[disc] = position
        audio = item["audio"]
        track_title = (music_text(edition_track.get("title")) or music_text(audio.get("title")) or
                       music_text(re.sub(r"^\d+[ ._-]+", "", Path(item["name"]).stem)))
        if not track_title:
            raise ValueError("A track has no usable title.")
        parent = destination / f"Disc {disc}" if multi else destination
        final = parent / f"{position:02d} {folder_name(track_title)}{source.suffix}"
        expected = tuple(item["fingerprint"])
        tags = {}
        previous_tags = {}
        if metadata_mode == "write":
            if source.suffix.casefold() != ".flac" or audio["codec"] != "flac":
                raise ValueError("Tag writing supports FLAC. Use Preserve downloaded files for this transfer.")
            tags = {"TITLE": track_title, "ARTIST": music_text(audio.get("artist")) or artist,
                    "ALBUMARTIST": artist, "ALBUM": title, "DATE": year,
                    "TRACKNUMBER": str(position), "DISCNUMBER": str(disc)}
            labels = (edition or {}).get("labels") or []
            for key, value in (("LABEL", ", ".join(label["name"] for label in labels if label["name"])),
                               ("CATALOGNUMBER", ", ".join(label["catno"] for label in labels if label["catno"]))):
                if value:
                    tags[key] = value
            roles = {"producer": "PRODUCER", "composer": "COMPOSER", "performer": "PERFORMER"}
            for role, tag in roles.items():
                names = [credit["name"] for credit in (edition or {}).get("credits", [])
                         if credit["role"].casefold() == role and credit["name"]]
                if names:
                    tags[tag] = ", ".join(names)
        with read_pinned(source, expected, identities) as stream:
            if tags:
                existing_tags = flac_metadata.music_tags(stream)
                previous_tags = {key: existing_tags.get(key) for key in tags}
                stream.seek(0)
            digest = keep_both.audio_digest(stream).hex()
        pairs.append({"flac": str(source), "flac_sig": expected, "download_digest": digest,
                      "install": str(final), "tags": tags, "before": previous_tags,
                      "title": track_title, "artist": music_text(audio.get("artist")) or artist,
                      "track": str(position), "disc": str(disc)})
    receipt = keep_both.staging_receipt_signature(job, pairs, identities)
    if not receipt:
        raise ValueError("The staged files no longer match their transfer receipt.")
    sizes = [pair["flac_sig"][0] for pair in pairs]
    if sum(sizes) + (max(sizes) if metadata_mode == "write" else 0) > shutil.disk_usage(root).free:
        raise ValueError("There is not enough free space in the selected library.")
    return {"candidate": candidate, "root": str(root), "identity": identity, "edition": edition,
            "details": details, "metadata_mode": metadata_mode, "directories": identities,
            "destination": str(destination), "new_folders": new_folders, "work": str(work),
            "staging": str(job), "receipt_signature": receipt, "pairs": pairs}


def preview(plan):
    return {key: plan[key] for key in ("candidate", "destination", "new_folders", "metadata_mode", "edition")} | {
        "files": [{key: pair[key] for key in ("install", "tags", "before")} for pair in plan["pairs"]],
        "token": keep_both.plan_token(plan),
    }


def write_tagged_copy(raw, raw_sig, path, tags, identities):
    with read_pinned(raw, raw_sig, identities) as source, keep_both.open_directory(path.parent, identities) as parent:
        descriptor = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(descriptor, "w+b") as output:
            flac_metadata.write_tags(source, output, tags)
            output.flush()
            os.fsync(descriptor)
            output.seek(0)
            actual = flac_metadata.music_tags(output)
            if any(actual.get(key) != value for key, value in tags.items()):
                raise ValueError("The written music tags could not be verified.")
            for stream in (source, output):
                stream.seek(0)
                flac_metadata.header(stream)
            if keep_both.audio_digest(source) != keep_both.audio_digest(output):
                raise ValueError("The audio changed while writing music tags.")
            output.seek(0)
            return keep_both.audio_digest(output), signature(os.fstat(descriptor))


def apply(db, reviewed):
    plan = prepare(db, reviewed["candidate"], reviewed["root"], reviewed["identity"],
                   reviewed["details"], reviewed["edition"])
    if keep_both.plan_token(plan) != keep_both.plan_token(reviewed):
        raise ValueError("The files, destination, or metadata setting changed. Review the import again.")
    root, work = Path(plan["root"]), Path(plan["work"])
    identities = dict(plan["directories"])
    created, published, copies = [], [], {}
    binary = find_probe()
    with closing(connect(db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if file_metadata_mode(connection) != plan["metadata_mode"]:
            raise ValueError("The file metadata setting changed. Review the import again.")
        children = check_catalog_root(root, plan["identity"], catalog_roots(connection))
        if connection.execute("SELECT 1 FROM kept_albums WHERE candidate=?", (plan["candidate"],)).fetchone():
            raise ValueError("This download is already in your library.")
        keep_both.create_directory(work, identities)
        journal = None
        try:
            journal = keep_both.write_journal(work, plan, identities)
            for directory in map(Path, plan["new_folders"]):
                keep_both.create_directory(directory, identities)
                created.append(directory)
            connection.execute("INSERT OR IGNORE INTO roots VALUES (?,?,?)", (str(root), *plan["identity"]))
            for child in children:
                prefix = str(child.relative_to(root)) + "/"
                connection.execute("UPDATE tracks SET root=?,path=? || path WHERE root=?",
                                   (str(root), prefix, str(child)))
                connection.execute("DELETE FROM roots WHERE path=?", (str(child),))
            columns = ("root", "path", *AUDIO_FIELDS, "size", "mtime", "device", "inode")
            for index, pair in enumerate(plan["pairs"]):
                raw = work / f"{index}.raw"
                staged = Path(pair["flac"])
                digest, copies[raw] = keep_both.copy_verified(staged, raw, pair["flac_sig"], identities)
                if digest.hex() != pair["download_digest"]:
                    raise ValueError("A staged file changed after review.")
                ready = raw
                if pair["tags"]:
                    ready = work / f"{index}.tagged"
                    digest, copies[ready] = write_tagged_copy(raw, copies[raw], ready, pair["tags"], identities)
                final = Path(pair["install"])
                fingerprint = keep_both.move_exclusive(ready, final, identities=identities, expected=copies[ready])
                del copies[ready]
                published.append((final, fingerprint))
                keep_both.verify_published(final, fingerprint, digest)
                with keep_both.open_directory(final.parent, identities) as directory:
                    audio = read_audio(binary, directory, final.name, fingerprint)
                    os.fsync(directory)
                if audio["quality"] != "lossless":
                    raise ValueError("The saved recording failed lossless inspection.")
                audio.update(title=pair["title"], artist=pair["artist"], album=plan["details"]["title"],
                             track_number=pair["track"], disc_number=pair["disc"])
                connection.execute(
                    f"INSERT INTO tracks({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                    (str(root), str(final.relative_to(root)), *(audio.get(key) for key in AUDIO_FIELDS), *fingerprint))
            result = {"destination": plan["destination"], "imported": len(published), "mode": "import",
                      "metadata_mode": plan["metadata_mode"], "metadata": plan["details"],
                      "edition": plan["edition"], "staging_removed": 0, "staging_cleared": False,
                      "staging_path": plan["staging"], "recovery_path": str(work)}
            connection.execute("INSERT INTO kept_albums(candidate,destination,mode) VALUES (?,?,'import')",
                               (plan["candidate"], plan["destination"]))
            connection.execute("INSERT INTO upgrade_results VALUES (?,?)", (plan["candidate"], json.dumps(result)))
            connection.commit()
        except BaseException:
            if not connection.in_transaction:
                raise ValueError(f"Review the recovery plan in {work} before retrying.")
            connection.rollback()
            try:
                for final, fingerprint in reversed(published):
                    keep_both.unlink_verified(final, fingerprint, identities)
                for directory in reversed(created):
                    keep_both.remove_empty_directory(directory, identities)
                keep_both.clean_work(work, identities, (), copies, journal)
            except (OSError, ValueError) as exc:
                raise ValueError(f"The import stopped. Review the recovery plan in {work} before retrying.") from exc
            raise
    removed, cleared = keep_both.remove_staged_copies({**plan, "directories": identities}, published)
    result.update(staging_removed=removed, staging_cleared=cleared, staging_path=None if cleared else plan["staging"])
    try:
        keep_both.clean_work(work, identities, (), copies, journal)
    except (OSError, ValueError):
        pass
    else:
        result["recovery_path"] = None
    with closing(connect(db)) as connection, connection:
        connection.execute("UPDATE upgrade_results SET result=? WHERE candidate=?", (json.dumps(result), plan["candidate"]))
    return result
