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

Try asking the connected chat to find up to three possible lossless editions of an album. The default ChatGPT connection exposes search. The dashboard can queue a selected result as described below.

### Discogs release lookup

The operator adds `--allow-discogs` to the Spliff MCP command, sets the app's `SPLIFF_DISCOGS_CONSUMER_KEY` and `SPLIFF_DISCOGS_CONSUMER_SECRET` in the local process environment, and refreshes the connection's tools in ChatGPT. Ask ChatGPT to connect Discogs. Open the approval link on the same Mac that runs Spliff, approve in Discogs, and return to chat. Spliff saves the resulting credential in an owner-only file under its Application Support directory. The callback listens only on that Mac and expires after five minutes.

For setup without environment variables, run `spliff discogs-setup` and open the one-time link on that Mac. Enter the application key and secret on the local page, then approve access on Discogs. Spliff stores the application credentials locally using the same owner-only storage.

For an existing personal-token setup, Spliff also accepts the `SPLIFF_DISCOGS_TOKEN` environment variable or the macOS Keychain item named `Spliff Discogs`. Chat requests take an artist and release title or a Discogs release ID. Results link to Discogs.

## Stage and review a search candidate

On the local dashboard, choose a music folder or an album folder to scan. Spliff remembers the scan. Find an MP3 album under **Albums**, click **Find FLACs**, and choose a source to download. Spliff remembers which indexed album you chose for the comparison.

You can also paste a candidate ID from Spliff Search under **Downloads → Add from chat**. IDs expire after ten minutes or when Nicotine+ restarts. A repeated queue request for the same saved ID returns its existing job. Spliff queues the observed audio files, one per numbered staging subfolder, and keeps the job under `~/Library/Application Support/Spliff/Staged downloads`. Nicotine+ manages pending and failed transfers.

A download appears when every selected file has finished. Spliff shows the album comparison first, with unmatched originals and preservation flags under **Track details**. For a download queued from chat, Spliff suggests an indexed album when one edition fits the tags. **Choose comparison album** selects a different edition. **Advanced** opens the track index and individual staged reviews.

An existing bridge needs `spliff install-nicotine-plugin --upgrade` followed by toggling **Spliff Search** off and on in Nicotine+. The bridge preserves Nicotine+ transfer filters. Turn off configured post-download commands and keep the staging and incomplete folders outside indexed library roots before queueing.

The ChatGPT connection can stay search-only while the dashboard handles queueing. Operators who explicitly want chat queueing can start MCP with `--allow-downloads` and refresh the custom plugin's tools. Point the MCP process at the intended catalog with `--db` before the subcommand. Downloads stay in staging for review.

### Keep both versions

When ChatGPT reviews a completed album with the download tools enabled, Spliff returns the **Keep both** or **Replace** question. The user chooses an option and confirms the destinations before anything in the library is changed. If the editions differ, Spliff reports the comparison issue and leaves the download staged.

The chat review also provides filing rules for the destination preview. Reuse an existing artist folder when the artist's identity is verified, including a former or current name. Otherwise, propose a new artist folder in the chosen library. File releases as `Artist/Category/[YYYY] Release Title`, using the year of the downloaded edition. Categories include Albums, Singles, EPs, Mixtapes, and Posthumous Albums. Deluxe reissues stay in Albums.

For multi-disc releases, the preview groups audio under `Disc 1`, `Disc 2`, and so on. Filenames restart at `01 Title.flac` on each disc and retain the source extension. Title tags contain only the title. Single-disc releases use the same filenames directly inside the release folder.

For an album with a complete one-to-one FLAC match against unprotected MP3 originals, click **Use FLACs** and choose **Keep both** or **Replace**. Either choice leads to a destination preview and a separate confirmation.

Spliff copies the staged FLACs into the album directory. **Keep both** moves the MP3s into `MP3`, preserving their indexed IDs. **Replace** sends the originals to the Mac Trash by default after Spliff verifies and indexes the FLACs. Choose a hidden recovery folder under **Advanced → Settings**, or ask ChatGPT to change the replacement preference when the MCP download tools are enabled. If the Trash move fails, Spliff reports where the original files are still saved. Once the saved FLACs pass byte-for-byte verification and the catalog update succeeds, Spliff removes the matching audio files from its Nicotine+ staging folder. Existing destinations and protected originals block the operation. If the Mac loses power during the operation, a hidden `.spliff-keep-both` directory in the album folder can contain the recovery plan. Review it before retrying.

## Spectral review

Spliff generates full-track spectrograms locally with the installed FFmpeg/FFprobe toolchain. No Spek, cloud service, Python package, or frontend library is required. Completed downloads show a graph automatically: choose a track and switch between **Original** and **Download**. **Advanced → Track index → Spectrum** opens the same viewer for an indexed original.

The graph uses the first audio stream, combined channel magnitudes, a Hann window, linear frequency up to Nyquist, and a −120 to 0 dBFS color scale. Each source uses its own sample rate and duration; read the axes when comparing different sources. It is visual evidence, not a test that proves lossless provenance. There is no automatic cutoff, clipping, or transcode verdict in this version.

Before a Keep both or Replace destination preview is offered, every original/download pair must finish decoding and produce a spectrogram. Failure blocks the preview and leaves files untouched. Existing metadata matching, protection, exact destination confirmation, and replacement settings still apply. Analysis is read-only; results are cached in process memory by file identity, size, and modification time. Restarting Spliff clears that cache. Tracks with unknown duration or over one hour are currently unsupported. Each analysis has a two-minute timeout.

MCP exposes `analyze_library_spectrum(track_id)` and, with downloads enabled, `analyze_download_spectrum(candidate_id, index=0, original_track_id=...)`. Responses contain PNG image blocks and metadata. A connected chat client receives the generated image, not audio. Search-only mode does not expose library analysis. The CLI uses the same implementation: `spliff spectrum TRACK_ID` prints JSON with metadata and a base64 PNG.
