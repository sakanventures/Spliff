# Spliff

Spliff is a local music catalog and lossless-upgrade review tool for macOS. It reads a mounted music folder, stores the index in SQLite, and serves a dashboard and local MCP tools. The dashboard has no playback controls.

## Start on a Mac

Requirements are Python 3.9 or newer and FFmpeg on your PATH. If you use Homebrew, install FFmpeg with `brew install ffmpeg`. Install from the project directory. For a first scan, choose a small music folder in the dashboard.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
spliff serve
```

Open [the local dashboard](http://127.0.0.1:8765) and click **Scan a folder** to open the macOS folder picker. The default catalog is `~/Library/Application Support/Spliff/catalog.sqlite`. To reuse an existing catalog, place `--db /path/to/catalog.sqlite` before the subcommand in every process. Keep the database and its SQLite sidecar files outside each indexed music folder. The web interface binds to loopback and is intended for the Mac running it.

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

Try asking the connected chat to find up to three possible lossless editions of an album. This milestone supports search. Download queueing and album replacement are future additions.
