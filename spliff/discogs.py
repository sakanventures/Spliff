"""Read-only Discogs release metadata for the local Spliff process."""
import json
import os
import re
import subprocess
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def personal_token():
    token = os.environ.get("SPLIFF_DISCOGS_TOKEN", "").strip()
    if not token and sys.platform == "darwin":
        result = subprocess.run(
            ["/usr/bin/security", "find-generic-password", "-s", "Spliff Discogs", "-w"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0:
            token = result.stdout.strip()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", token):
        raise ValueError("Discogs is not connected. Add a personal access token on the Spliff host.")
    return token


def request_discogs(path, authorization, params=None, method="GET"):
    try:
        url = "https://api.discogs.com" + path + ("?" + urlencode(params) if params else "")
        request = Request(url, headers={
            "Authorization": authorization,
            "User-Agent": "Spliff/0.1.0 +https://github.com/sakanventures/Spliff",
            "Accept": "application/json" if not path.startswith("/oauth/") else "*/*",
        }, method=method)
        with build_opener(NoRedirect()).open(request, timeout=10) as reply:
            return reply.read()
    except HTTPError as exc:
        with exc:
            if exc.code == 429:
                raise ValueError("Discogs is rate limiting requests. Try again shortly.") from exc
            if exc.code in (401, 403):
                raise ValueError("Discogs refused access. Check the Spliff connection on your Mac.") from exc
            if exc.code == 404:
                raise ValueError("Discogs could not find that release.") from exc
        raise ValueError("Discogs could not complete the lookup.") from exc
    except (URLError, TimeoutError) as exc:
        raise ValueError("Spliff could not complete the Discogs lookup.") from exc


def fetch(path, params=None):
    from .discogs_auth import saved_authorization

    try:
        authorization = saved_authorization() or "Discogs token=" + personal_token()
        return json.loads(request_discogs(path, authorization, params))
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise ValueError("Spliff could not complete the Discogs lookup.") from exc


def search_releases(_db, artist, title, limit=5):
    if any(not isinstance(value, str) or not 1 <= len(value.strip()) <= 180
           for value in (artist, title)) or type(limit) is not int or not 1 <= limit <= 10:
        raise ValueError("Enter an artist and release title, and choose up to 10 results.")
    payload = fetch("/database/search", {"artist": artist.strip(), "release_title": title.strip(),
                                         "type": "release", "per_page": limit})
    items = []
    for result in payload["results"][:limit]:
        release_id = result["id"]
        items.append({**{key: result.get(key) for key in ("title", "year", "format", "label", "catno", "country")},
                      "id": release_id, "url": f"https://www.discogs.com/release/{release_id}"})
    return {"attribution": "Data provided by Discogs.", "items": items}


def release_details(_db, release_id):
    if type(release_id) is not int or not 1 <= release_id < 2**63:
        raise ValueError("Choose a valid Discogs release ID.")
    result = fetch(f"/releases/{release_id}")
    return {"attribution": "Data provided by Discogs.", "id": release_id,
            "url": f"https://www.discogs.com/release/{release_id}",
            **{key: result.get(key) for key in ("title", "year", "released", "formats", "country")},
            "artists": [artist.get("name") for artist in result.get("artists", [])],
            "labels": [{key: label.get(key) for key in ("name", "catno")}
                       for label in result.get("labels", [])],
            "tracklist": [{key: track.get(key) for key in ("position", "title", "duration", "type_")}
                          for track in result.get("tracklist", [])]}
