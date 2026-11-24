# Spliff

The native Mac companion is in `apple/Spliff/Spliff.xcodeproj`. It opens the dashboard inside the app and manages the background connection through the existing service operations. See [Mac companion setup](apple/README.md) for the development build and current distribution limits.

Spliff is a local music catalog and lossless-upgrade review tool for macOS. It reads a mounted music folder, stores the index in SQLite, and serves a dashboard and local MCP tools. The dashboard has no playback controls.

## Start on a Mac

Requirements are Python 3.9 or newer and FFmpeg on your PATH. If you use Homebrew, install FFmpeg with `brew install ffmpeg`. Install from the project directory. For a first scan, choose a small music folder in the dashboard.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
spliff serve
```

Open [the local dashboard](http://127.0.0.1:8765) and choose **Sync → Choose music folder** to open the macOS folder picker. The default catalog is `~/Library/Application Support/Spliff/catalog.sqlite`. To reuse an existing catalog, place `--db /path/to/catalog.sqlite` before the subcommand in every process. Keep the database and its SQLite sidecar files outside each indexed music folder. The web interface binds to loopback and is intended for the Mac running it.

The first scan opens each recognized audio file for metadata inspection. Later scans reuse successful probes when size, modification time, device, and inode agree. Catalog changes become visible when each scan commits. An interrupted or failed walk keeps the previous catalog. An unavailable or replaced root requires operator review. Probe failures appear under Inspection errors and are retried on the next scan. Hidden files, hidden folders, and symlink entries are skipped. Overlapping roots are rejected. Search currently uses SQLite substring matching, with case-insensitive matching for ASCII.

## Preserve and stage

Find a track in the dashboard and click **Protect** to flag it. To pair it with a lossless candidate, click **Compare file** and drop one file onto the dialog or use **Choose a file**. Spliff inspects a temporary local copy and removes that copy after saving review metadata. Audio files stay in their original locations. Candidate files may be up to 512 MiB.

The CLI also supports `spliff scan FOLDER`, `spliff protect ID`, and `spliff stage ID FILE`. For CLI staging, keep the candidate outside the indexed music folders.

Open **Staged reviews** to compare tags, codec, duration, sample rate, and bit depth. A preservation flag belongs to an indexed root and relative path and survives a missing-file scan. If you move or rename a protected recording, flag the new entry after scanning. Stage checks that the original still matches its indexed file attributes and inspects the candidate codec. Reviews store observations from the time of staging. Compare the recording, edit, and mastering manually before treating a candidate as an upgrade. Use `unprotect ID` to remove a flag.

Spliff leaves the original and candidate files in place. Lossless reflects the reported codec. The provenance of the source audio remains unknown.

## Local MCP

Set the MCP client's command to the absolute path of the installed `spliff` executable, such as `/absolute/path/to/project/.venv/bin/spliff`. Use the following arguments and point to the same catalog as the dashboard.

```json
["--db", "/absolute/path/catalog.sqlite", "mcp"]
```

The server implements legacy MCP stdio negotiation through version `2025-11-25`. Its tools are `library_summary`, `search_library`, and `list_staged_reviews`. A connected client can receive track metadata and library-relative paths.

## Development and license

See [Contributing](CONTRIBUTING.md) for the development setup and tests, and the [integration plan](INTEGRATIONS.md) for upstream interfaces and compatibility checks. Spliff is licensed under the [MIT License](LICENSE).

The initial release targets macOS. The vertical slice has been exercised with generated audio. A multi-terabyte benchmark, live Soulseek transfer validation, and broader MCP client testing are still pending.

## Search Soulseek from chat

Spliff Search uses an installed, connected copy of Nicotine+ 3.3.11. Install the bridge once, then enable **Spliff Search** in Nicotine+ preferences under **Plugins**. Keep Nicotine+ running and signed in while searching.

```sh
spliff install-nicotine-plugin
spliff nicotine-status
```

If Nicotine+ uses a custom plugin folder, pass its path with `--plugins-dir`. The installer leaves an existing bridge installation intact.

The `search_nicotine` MCP tool accepts a search query, a track or album mode, and an optional result limit. It returns peer-advertised lossless candidates from an eight-second search. Album results describe matching files seen in that window. They do not confirm a complete album or its edition.

For a local MCP client, use the installed Spliff executable with arguments `mcp --search-only`. For standard ChatGPT, use the [OpenAI Secure MCP Tunnel stdio setup](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels) with the absolute Spliff executable and the same arguments as the MCP command. Use a Platform tunnel and runtime key associated with the ChatGPT workspace, then add the tunnel as a custom MCP connection in ChatGPT. The search-only command excludes the catalog tools. The dashboard stays local. Search terms go to Soulseek and candidate metadata goes to the connected chat.

Try asking the connected chat to find up to three possible lossless editions of an album. The default ChatGPT connection exposes search. The dashboard can queue a selected result as described below.

### Discogs release lookup

The operator adds `--allow-discogs` to the Spliff MCP command, sets the app's `SPLIFF_DISCOGS_CONSUMER_KEY` and `SPLIFF_DISCOGS_CONSUMER_SECRET` in the local process environment, and refreshes the connection's tools in ChatGPT. Ask ChatGPT to connect Discogs. Open the approval link on the same Mac that runs Spliff, approve in Discogs, and return to chat. Spliff saves the resulting credential in an owner-only file under its Application Support directory. The callback listens only on that Mac and expires after five minutes.

For setup without environment variables, run `spliff discogs-setup` and open the one-time link on that Mac. Enter the application key and secret on the local page, then approve access on Discogs. Spliff stores the application credentials locally using the same owner-only storage.

For an existing personal-token setup, Spliff also accepts the `SPLIFF_DISCOGS_TOKEN` environment variable or the macOS Keychain item named `Spliff Discogs`. Chat requests take an artist and release title or a Discogs release ID. Results link to Discogs.

## Stage and review a search candidate

In **Sync**, choose a local music folder. Spliff scans it, schedules searches for owned lossy recordings, stages suitable peer offers through Nicotine+, and reviews completed audio. The local dashboard service advances the queue even when the window is closed. **Downloads** shows transfers that still have a staging folder. Downloads waiting for a choice remain available across restarts. The catalog retains the transfer ID and outcome after a completed transfer leaves staging.

Open a completed download and choose **Choose upgrade** to see the available plans. **Keep both** retains originals in a visible archive alongside the new files. **Replace** moves upgraded originals to Mac Trash or the hidden recovery folder selected in Settings. The mode button authorizes the displayed plan. Spliff checks the plan and both sets of file hashes before applying it. Changes to the inputs or destinations require a new choice.

Spliff verifies and indexes the installed lossless files before archiving originals. It skips unresolved, protected or conflicting tracks and reports the counts. Mac Trash receives individual reviewed originals. If a move to Trash fails, Spliff reports the folder holding the remaining originals. Before installing anything, Spliff records each original, recovery and installation path in `.spliff-keep-both/plan.json` inside the album. After an interruption, use that plan to locate the files before retrying. Cleanup removes verified Spliff files and empty Spliff-created folders, leaving unexpected files for inspection.

Use **Advanced** to search manually, inspect editions, compare individual files or open spectrograms. Nicotine+ post-download commands must be turned off, and its staging and incomplete folders must be outside indexed music folders. For older bridge installations, run `spliff install-nicotine-plugin --upgrade` and reload Spliff Search in Nicotine+.

## Keep both versions

When ChatGPT reviews a completed album with the download tools enabled, Spliff returns both plans and a link to the local approval screen. Open it on the Mac running Spliff to see the exact destinations and choose **Keep both** or **Replace**. The local choice applies once to the displayed plan. If the files, destinations or replacement preference change, reopen the review and choose again.

The chat review also provides filing rules for the destination preview. Reuse an existing artist folder when the artist's identity is verified, including a former or current name. Otherwise, propose a new artist folder in the chosen library. File releases as `Artist/Category/[YYYY] Release Title`, using the year of the downloaded edition. Categories include Albums, Singles, EPs, Mixtapes, and Posthumous Albums. Deluxe reissues stay in Albums.

For multi-disc releases, the preview groups audio under `Disc 1`, `Disc 2`, and so on. Filenames restart at `01 Title.flac` on each disc and retain the source extension. Title tags contain only the title. Single-disc releases use the same filenames directly inside the release folder.

For an album with a complete one-to-one FLAC match against unprotected MP3 originals, click **Use FLACs** and choose **Keep both** or **Replace**. Either choice leads to a destination preview and a separate confirmation.

Spliff copies the staged FLACs into the album directory. **Keep both** moves the MP3s into `MP3`, preserving their indexed IDs. **Replace** sends the originals to the Mac Trash by default after Spliff verifies and indexes the FLACs. Choose a hidden recovery folder under **Advanced → Settings**, or ask ChatGPT to change the replacement preference when the MCP download tools are enabled. After either choice succeeds, Spliff removes the verified duplicates from Nicotine+ staging. Once the transfer is fully installed and the staging folder is empty of audio, Spliff removes the staging receipt and folder. Files skipped in the install and unexpected files stay in staging. If the Trash move fails, Spliff keeps the staged audio and reports where the original files are still saved. If the Mac loses power during the operation, a hidden `.spliff-keep-both` directory in the album folder can contain the recovery plan. Review it before retrying.

## Add a first-time download

Ask ChatGPT to download a specific album or track by artist, then select a search result. An indexed original is optional. After the download finishes, use **Add to library** from the review link or the local dashboard. Choose your music library and review the artist, category, edition year, destination folders, and filenames. Spliff reuses a matching artist folder when it can verify the name and creates missing folders after approval. If the album’s first release was after the artist’s death, confirm that when selecting **Posthumous Albums**.

When Discogs is connected and the downloaded tags and tracklist match, the review offers editions with their year, release type, label, and catalog number. Choose the edition you downloaded or supply the filing details yourself. Spliff saves music metadata from the selected edition without saving its links. Under **Settings → File metadata on import**, **Preserve downloaded files** keeps the original bytes and tags. **Write verified music tags (FLAC)** previews and writes the shown FLAC tags while preserving the audio bytes and existing artwork. Use preserve mode for other formats.

Approving **Add to library** verifies and indexes the installed audio before clearing its staged copies. A changed plan or existing destination requires a new review. An interrupted install leaves the staged audio available and can leave a `.spliff-import-…` recovery plan in the selected library.

## Spectral review

Spliff generates full-track spectrograms locally with the installed FFmpeg/FFprobe toolchain. No Spek, cloud service, Python package, or frontend library is required. Completed downloads show a graph automatically: choose a track and switch between **Original** and **Download**. **Advanced → Track index → Spectrum** opens the same viewer for an indexed original.

The graph uses the first audio stream, combined channel magnitudes, a Hann window, linear frequency up to Nyquist, and a −120 to 0 dBFS color scale. Each source uses its own sample rate and duration; read the axes when comparing different sources. It is visual evidence, not a test that proves lossless provenance. There is no automatic cutoff, clipping, or transcode verdict in this version.

Spliff decodes and generates spectral evidence for each proposed pair. It compares spectral rolloff and the audio envelope of up to five minutes. The envelope can help resolve conflicting track positions or guest credits when the title, primary artist, release and duration agree. An unresolved recording or failed decode stays out of the plan. Discogs tracklists can corroborate a release when the connection is available. Neither advertised codecs nor these measurements establish genuine lossless provenance.

MCP exposes `analyze_library_spectrum(track_id)` and, with downloads enabled, `analyze_download_spectrum(candidate_id, index=0, original_track_id=...)`. Responses contain PNG image blocks and metadata. A connected chat client receives the generated image, not audio. Search-only mode does not expose library analysis. The CLI uses the same implementation: `spliff spectrum TRACK_ID` prints JSON with metadata and a base64 PNG.

## Artist-name workflow in ChatGPT

Use the catalog-enabled MCP connection for requests such as **Common**. It inventories the artist's indexed music first, then searches for upgrades of the owned releases. It does not substitute a Soulseek discography for the user's collection. The artist inventory lists MP3/other formats, observed bitrates, track counts, protected tracks, album folders, and loose cuts. Disc folders are grouped into their parent release; loose files at the artist level remain tracks even when their album tags contain an album name or website.

`artist_library(artist)` reads the inventory. `search_artist_upgrades(artist)` searches up to three owned releases per batch and returns up to five candidates for each. Albums appear before loose tracks. Use the returned `next` offset as `after` to review another batch when requested. Search failures and searches with no name-matching results are distinct. Protected, already lossless, and uninspected releases are not searched. Artist and release words in peer paths are conservative matching clues only. They do not verify recording identity, edition, or completeness. This version supports lossless searches; higher-bitrate MP3 fallback is still pending.

If the artist is absent, select or supply the artist's exact local folder and call `index_music_folder(folder)`. This records metadata in the catalog without changing audio. Search and indexing are separate operations. After selecting a candidate, `stage_nicotine_download(candidate_id, original_track_id=RELEASE_ID)` retains the owned release's comparison for later review. This manual path remains available in Advanced. Use `sync_music_folder` to search a selected folder for upgrade candidates and review transfers you explicitly select.

**Connection setup matters:** `mcp --search-only` deliberately exposes only remote Soulseek search. It cannot provide the artist workflow. Start the connection with the actual populated catalog and without `--search-only`, for example:

```sh
spliff --db /absolute/path/catalog.sqlite mcp --allow-downloads
```

Restart the MCP process/tunnel and refresh the custom plugin's tools. Confirm that `artist_library`, `search_artist_upgrades`, and `index_music_folder` appear before expecting a bare artist name to inspect owned music. Library paths and metadata are returned to the connected chat. The default `search_nicotine` operation on this catalog-enabled connection routes exact indexed artist names through the same artist operation. With an unindexed name, its default auto mode asks to index the folder; explicit `kind=track` or `kind=album` still supports remote discovery.

The CLI calls the same operations with `spliff --db PATH artist-library Common` and `spliff --db PATH artist-upgrades Common`. The dashboard API exposes `GET /api/artist-library?artist=Common` and an explicitly requested `POST /api/artist-upgrades?artist=Common` using the existing dashboard action headers.

## Background Mac services

`Install-Background-Services.command` is the one-time installer for this checkout. Stop the existing Terminal tunnel with **Control+C**, then double-click the installer (or run `./Install-Background-Services.command`). It uses `artist-workflow.sqlite`, which contains the Common artist scan, for both the dashboard and MCP. Leave the project and its virtual environment at the same paths after installation.

The installer creates two user login agents: `com.sakanventures.spliff.dashboard` and `com.sakanventures.spliff.tunnel`. They run after login, restart after failure, and need no Terminal window. The dashboard listens at <http://127.0.0.1:8765>. The tunnel agent supervises the MCP child, enables library tools, downloads that the user selects, and Discogs lookups, and preserves the existing tunnel ID. Nicotine+ must still be running, connected, and have Spliff Search enabled. The Mac must be awake and the music drive mounted for the corresponding operations. These are per-user login services, not pre-login system daemons.

The tunnel runtime key is entered once with hidden input and saved in an owner-only file under `~/Library/Application Support/Spliff/Services`. Do not paste it into chat. Neither the plists nor source code contain the key. The original tunnel profile remains unchanged; the installer makes a service profile that references the saved key and uses the catalog-enabled MCP command. Refresh the Spliff connection tools in ChatGPT after the tunnel is ready.

Use these commands from the project directory for maintenance:

```sh
.venv/bin/python -m spliff services status
.venv/bin/python -m spliff services restart
.venv/bin/python -m spliff services stop
.venv/bin/python -m spliff services uninstall
```

`status` reports loaded agents, running processes, dashboard readiness, and tunnel readiness after a successful control-plane poll. Loading an agent alone does not establish that the tunnel is connected. Logs are in `~/Library/Application Support/Spliff/Services` (`dashboard.log`, `dashboard.error.log`, `tunnel.log`, `tunnel.error.log`). Uninstall retains the catalog, audio, logs, and credential. To change the saved credential, rerun install with `--replace-key`.

To choose another catalog or dashboard port, run `.venv/bin/python -m spliff --db /absolute/path/catalog.sqlite services install --port 8765`. Status and restart use the saved service configuration. Installation refuses an occupied dashboard port and backs up matching existing agent files. A failure while loading the agents restores previous agent files and attempts to restart previously loaded agents. `services prepare --output background-services` generates reviewable files without installing agents or requesting a credential.

### ChatGPT tool registration and selected downloads

Changing local MCP code does not update an already registered ChatGPT tool schema. After restarting the Spliff tunnel's MCP child, open Spliff's plugin settings and use **Refresh tools**. The plugin's app details should show the staging, status and review tools. Existing conversations may still carry their original tool snapshot. Test the refreshed plugin in a new conversation.

With downloads enabled, `sync_music_folder` schedules searches for the selected local folder. Review matching offers and explicitly select each download. `music_sync_status` shows progress. `list_nicotine_transfers` exposes the same transfer inventory as Downloads, including work queued through the GUI. `list_staged_reviews` covers manual file comparisons only.

The upgraded Nicotine+ bridge starts with Spliff staging disabled. After reviewing Nicotine+'s saved queue, run `spliff nicotine-downloads enable` locally to permit explicitly selected candidates. Use `spliff nicotine-downloads stop` to disable staging and pause unfinished transfers matched to Spliff manifests. If the command cannot confirm pauses, disconnect Nicotine+ and check the queue. Spliff leaves unrelated transfers alone. Intent and submission records are written to the private `transfer-audit.jsonl` in Spliff's staging directory.

Call `review_nicotine_download` to get both reviewed plans with exact paths, preserved and skipped originals, and the local approval link. Present the plans and link so the user can choose on their Mac. Check `nicotine_transfer_status` for the result. When the dashboard runs on a custom port, start MCP with the same `--dashboard-port` value.
