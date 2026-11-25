# MCP library review

Spliff's earlier MCP adapter withheld plan tokens and sent every file approval to the dashboard. The earlier Keep both planner shared replacement's matching gate and moved matched lossy originals into an MP3 subfolder. The spectral renderer already emitted PNGs, and the MCP adapter already returned standard image content blocks. Display depends on the client and tunnel carrying those blocks through to the user.

## Tool contracts

| Tool | Arguments | Result |
| --- | --- | --- |
| `review_nicotine_download` | `candidate_id`, optional `original_track_id` | Review, format counts, additional-track candidates, blocked actions and exact reviewed options. |
| `preview_album_action` | `candidate_id`, `mode` (`keep` or `replace`), optional `original_track_id`, `library_root`, `override_track_ids` | A scoped plan ID valid for 15 minutes, destinations, and any explicitly selected recording exceptions. Replace requires an original ID. |
| `apply_album_action` | `candidate_id`, `mode`, `plan_id`, original ID when present in the review | Host confirmation followed by execution. Returns exact installed and original paths, archive locations, verification, skipped files, transaction ID and recovery status. |
| `inspect_album_recovery_journal` | `candidate_id` | Read-only inspection of any matching recovery journal and its planned file paths. |
| `get_album_action_status` | `candidate_id` | Committed result, including rollback availability or the path to a recovery journal. |
| `rollback_album_action` | `candidate_id`, `transaction_id` | Host-confirmed restore of hidden-archive originals. Retains the installed recordings in a named recovery folder. |
| `analyze_download_spectrum` | `candidate_id`, optional zero-based `index` and `original_track_id` | PNG image content blocks and numerical metadata for the selected download and its matched original. |
| `nicotine_transfer_status` | `candidate_id` | Existing Nicotine+ file states, normalized states when supported, and recovery tool names. |
| `find_nicotine_alternatives` | `candidate_id` | Fresh search using the saved release query. Peer claims require review. |
| `authorize_nicotine_retry` | `candidate_id` | Authorizes the exact paused Nicotine+ rows for a manual retry. Existing active transfers are left to Nicotine+. |

## Authorization and recovery

Write-enabled connections advertise Apply and Rollback. Execution requires the active MCP session to negotiate form elicitation. Spliff creates the confirmation request and checks the matching JSON-RPC response, explicit acceptance and affirmative form value. Tool arguments carry no confirmation field. Use this private, single-user tunnel with a trusted client. A client holding direct access to the MCP process is part of the trust boundary.

The catalog stores the exact reviewed operation, the staged snapshot and the operation's expiry. The existing import and replacement engines enforce directory identities, exclusive publication, file digests and catalog transactions. A repeated successfully committed plan returns its recorded result. A different plan cannot apply the same candidate again. Replacement publishes and verifies the download before archiving the original. Hidden-folder rollback restores originals without overwriting a newly occupied path. Mac Trash names are assigned by macOS and automatic rollback is available only for hidden-folder recovery. If work is interrupted and a journal remains, inspect its paths before retrying.

Keep both files the full downloaded release as its own edition using the library's configured folder style and file metadata policy. It performs a full decode and leaves original files at their paths. An existing release directory blocks import. Replace requires waveform alignment and checks for conflicting envelope evidence. An advanced exception can admit a uniquely matched track with duration or release-tag discrepancies, subject to the same integrity, protected-file and destination checks. The evidence retains its uncertainty.

OpenAI documents support for ChatGPT write actions, says destructive tool hints prompt the host to elicit approval, and requires servers to enforce authorization independently. The public tool-call documentation describes no signed per-call permission token that Spliff could validate. MCP form elicitation supplies a protocol exchange with the connected client. Spliff uses a local approval page for Keep both and Replace when the connected host has no form capability. The page approves and executes the exact plan in one step. [OpenAI custom MCP server guide](https://developers.openai.com/api/docs/guides/custom-mcp-server) · [OpenAI tool annotations](https://developers.openai.com/plugins/reference) · [OpenAI server guide](https://developers.openai.com/plugins/build/mcp-server) · [MCP elicitation specification](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation)

## Manual beta check

1. Use a disposable indexed library and a separate private staging directory. Start the existing Spliff tunnel in download-enabled mode with that catalog and refresh its connection in ChatGPT.
2. Search for a fixture release, select a candidate, stage it, and wait for the transfer to finish. Ask ChatGPT to show both available actions and their destinations. Ask for the original and downloaded spectrograms for one track.
3. Choose Keep both, decline the host form and check the library. Repeat and accept. Check that the old files stayed in place and that the new edition and the extra track were indexed. Confirm that repeating the same plan reports the same transaction.
4. Use a second fixture for verified Replace. Set the originals destination to the hidden recovery folder, accept the exact paths, then use the transaction ID to roll it back. Check that the original was restored and the downloaded replacement remains at the reported recovery path.
5. Exercise a disconnected peer or paused transfer. Check the reported Nicotine+ state and use the alternative search with a fresh candidate selection. A paused transfer can be authorized for exact-row retry in Nicotine+.

The regression fixtures use generated audio and temporary directories. The live Buju Banton music files are outside the test scope. Spectrum plots and waveform evidence support review. Source authenticity, exact pressing identification, acoustic fingerprints, crash replay and client-side plot rendering are separate verification limits.

## Live ChatGPT confirmation compatibility

The installed Secure MCP Tunnel client was version `0.0.21+714f2d67844b660af25754f1d19b0360b346b13b`, with the main channel connected to Spliff over stdio. ChatGPT's **Refresh tools** succeeded for Spliff 0.1.2. In a fresh ChatGPT chat, the tool `spliff_confirmation_diagnostics(probe=true)` returned the following server-observed details.

| Field | Live value |
| --- | --- |
| Offered protocol | `2025-11-25` |
| Negotiated protocol | `2025-11-25` |
| Client capability names | `experimental`, `extensions` |
| Core form capability | Absent |
| OpenAI form extension | Absent |
| Form method | None |
| Probe | `no_negotiated_form` |

Spliff 0.1.2 recognizes the OpenAI legacy form extension when the negotiated client explicitly advertises it. The harmless probe only requests a form when the server has observed an advertised mode. The MCP server writes a minimal capability snapshot to stderr at initialization and excludes client identifiers, headers, credentials, and album data from that log. Legacy OpenAI forms use `openai/elicitation/create`. OpenAI documents that registered MCP servers require MCP `2026-07-28` or later with multi-round-trip input for forms. The currently installed connection offered the older protocol. [OpenAI form extension](https://github.com/openai/mcp-extensions/blob/main/docs/spec.md#openai-form-elicitation) · [MCP multi-round-trip requests](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr)

Host tool approval remains useful as a front-end permission control. The public contract documents host approval prompts and widget tool-input delivery after approval. It describes no server-verifiable, action-bound approval receipt on the ordinary tool call. MCP Apps widgets can show reviewed paths and call app-visible tools through the host bridge. Widget-only routing and hidden result metadata control presentation and visibility. The documented interface does not provide a signed user gesture or approval receipt. A widget could participate in a flow with an independently authenticated, action-bound approval mechanism. A server should still enforce that proof and the reviewed operation on every request. [OpenAI plugin reference](https://developers.openai.com/plugins/reference) · [OpenAI widget bridge](https://developers.openai.com/plugins/build/chatgpt-ui) · [OpenAI server authorization guidance](https://developers.openai.com/plugins/build/mcp-server)

The current live verification establishes the capability gap through ChatGPT itself. A newer MCP transport would need live verification of `server/discover`, per-request capabilities, the input-required exchange, and a harmless accept/decline probe before using it for library operations. The earlier album apply attempt returned the missing-capability error and its status was `no_committed_action`. The confirmation compatibility probe made no attempt to import, replace, or roll back any files.


## Confirmation through the local Spliff page

Spliff 0.1.3 routes verified review plans through a negotiated core or OpenAI legacy form when supported. Otherwise it returns an approval link to the existing loopback dashboard. ChatGPT should show the selected plan's link directly. The page lists staged, original, installation and archive paths, and identifies recording exceptions. **Approve** executes that plan once. **Decline** consumes the displayed form and changes no library files. ChatGPT reads `get_album_action_status` afterward.

The link identifies an expiring plan. Loading it only displays the review and issues a fresh local form nonce. Approval requires the exact loopback Host and Origin, the nonce, and the matching stored plan digest. The server consumes the plan's authorization before attempting file changes, including when a later revalidation fails. The receipt for a committed plan remains readable on retries. Hidden-folder rollback still requires native form confirmation. Interrupted operations can be inspected with `inspect_album_recovery_journal`; journal replay is manual.

## MCP 2026-07-28 live test

Spliff 0.1.4 implements per-request MCP metadata and multi-round-trip form confirmation for both core MCP and the OpenAI form extension. A prompted call returns a random, short-lived continuation that binds the tool, arguments, form dialect and exact reviewed plan. The server consumes it on the matching continuation. Existing transaction validation and single-use album authorization still apply. A tunnel restart expires pending prompts.

The installed ChatGPT connector successfully discovered and invoked Spliff using MCP `2026-07-28`. Its live `spliff_confirmation_diagnostics(probe=true)` request advertised `experimental` and `extensions`, with neither core nor OpenAI form elicitation. The result was `no_negotiated_form` and the route remained `local_approval`. No music or staged files were changed during the live test. The in-chat form exchange is covered by local protocol tests and requires a live client that advertises form support before it can be verified end to end in ChatGPT. [MCP multi-round-trip requests](https://modelcontextprotocol.io/specification/2026-07-28/basic/patterns/mrtr) · [OpenAI form extension](https://github.com/openai/mcp-extensions/blob/main/docs/spec.md#openai-form-elicitation)

## Recording verification and batch spectra

Replacement review uses the embedded title and credits when available, then checks normalized filenames, known artist and album prefixes, disc and track position, and release context. The position can nominate a recording for comparison. It cannot establish recording identity. Explicit version conflicts, ambiguous duplicates, protected originals, and failed audio checks remain excluded. When enabled, sync or `review_nicotine_download(research_edition=true)` can corroborate edition details using Discogs.

The strict replacement review first attempts waveform alignment. With local `fpcalc` installed, an inconclusive waveform result can be corroborated by a near-full-length Chromaprint match and a similar audio envelope. A short, weak, or conflicting fingerprint stays blocked. This checks recording identity independently of the fidelity of the FLAC. Candidate files are decoded before approval and verified again on installation. [Chromaprint](https://acoustid.org/chromaprint)

Use `analyze_album_spectrum(candidate_id, original_track_id?, start?, limit?)` to start a background batch. Poll `album_spectrum_status(job_id)` for the result and each track's status. A repeat request can target a range using its zero-based `start` and `limit`. The process caches successful results by file identity, size, modification time, and change time. Use the returned track index with `analyze_download_spectrum` to open the cached individual images. The batch reports rolloff, band energy, and possible low-pass boundaries. A low-pass indicator can reflect mastering or prior encoding and does not identify a lossy transcode. Jobs and cached images expire on a service restart.

The tool manifest changed in 0.1.5. Restart the local dashboard and tunnel after installing the revised package, then refresh Spliff's tools in ChatGPT. Install `fpcalc` where the local services can find it to enable the optional fingerprint comparison.

### Roy Ayers regression

The four staged albums were reprocessed with a disposable catalog and the official Chromaprint 1.6.2 binary. The baseline came from the same staged files with the previous verifier.

| Album | Tracks | Eligible before | Eligible with Chromaprint |
| --- | ---: | ---: | ---: |
| In the Dark | 11 | 0 | 11 |
| Love Fantasy | 6 | 0 | 0 |
| Nasté | 13 | 8 | 12 |
| Wake Up | 7 | 2 | 6 |

The filename fallback resolved missing tags on *In the Dark*. *Love Fantasy* now reaches acoustic comparison for all six tracks, but the fingerprints fall below the threshold. The *Nasté* track “Nonsense” exceeds the alignment window, and its fingerprint has too many unmatched frames. “Suavé” on *Wake Up* reaches comparison after normalizing the accent, but its envelope does not corroborate the fingerprint strongly enough for automatic replacement.

The batch analyzed all 37 downloads and compared all 37 against their corresponding originals. These files produced no abrupt-band-drop flags under the current heuristic. This result provides no guarantee of source provenance.
