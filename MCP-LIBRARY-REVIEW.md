# MCP library review

Spliff's earlier MCP adapter withheld plan tokens and sent every file approval to the dashboard. The earlier Keep both planner shared replacement's matching gate and moved matched lossy originals into an MP3 subfolder. The spectral renderer already emitted PNGs, and the MCP adapter already returned standard image content blocks. Display depends on the client and tunnel carrying those blocks through to the user.

## Tool contracts

| Tool | Arguments | Result |
| --- | --- | --- |
| `review_nicotine_download` | `candidate_id`, optional `original_track_id` | Review, format counts, additional-track candidates, blocked actions and exact reviewed options. |
| `preview_album_action` | `candidate_id`, `mode` (`keep` or `replace`), optional `original_track_id`, `library_root`, `override_track_ids` | A scoped plan ID valid for 15 minutes, destinations, and any explicitly selected recording exceptions. Replace requires an original ID. |
| `apply_album_action` | `candidate_id`, `mode`, `plan_id`, original ID when present in the review | Host confirmation followed by execution. Returns exact installed and original paths, archive locations, verification, skipped files, transaction ID and recovery status. |
| `get_album_action_status` | `candidate_id` | Committed result, including rollback availability or the path to a recovery journal. |
| `rollback_album_action` | `candidate_id`, `transaction_id` | Host-confirmed restore of hidden-archive originals. Retains the installed recordings in a named recovery folder. |
| `analyze_download_spectrum` | `candidate_id`, optional zero-based `index` and `original_track_id` | PNG image content blocks and numerical metadata for the selected download and its matched original. |
| `nicotine_transfer_status` | `candidate_id` | Existing Nicotine+ file states, normalized states when supported, and recovery tool names. |
| `find_nicotine_alternatives` | `candidate_id` | Fresh search using the saved release query. Peer claims require review. |
| `authorize_nicotine_retry` | `candidate_id` | Authorizes the exact paused Nicotine+ rows for a manual retry. Existing active transfers are left to Nicotine+. |

## Authorization and recovery

Apply and rollback are advertised only when a write-enabled MCP session negotiates form elicitation. Spliff creates the confirmation request and checks the matching JSON-RPC response, explicit acceptance and affirmative form value. Tool arguments carry no confirmation field. Use this private, single-user tunnel with a trusted client. A client holding direct access to the MCP process is part of the trust boundary.

The catalog stores the exact reviewed operation, the staged snapshot and the operation's expiry. The existing import and replacement engines enforce directory identities, exclusive publication, file digests and catalog transactions. A repeated successfully committed plan returns its recorded result. A different plan cannot apply the same candidate again. Replacement publishes and verifies the download before archiving the original. Hidden-folder rollback restores originals without overwriting a newly occupied path. Mac Trash names are assigned by macOS and automatic rollback is available only for hidden-folder recovery. If work is interrupted and a journal remains, inspect its paths before retrying.

Keep both files the full downloaded release as its own edition using the library's configured folder style and file metadata policy. It performs a full decode and leaves original files at their paths. An existing release directory blocks import. Replace requires waveform alignment and checks for conflicting envelope evidence. An advanced exception can admit a uniquely matched track with duration or release-tag discrepancies, subject to the same integrity, protected-file and destination checks. The evidence retains its uncertainty.

OpenAI documents support for ChatGPT write actions, says destructive tool hints prompt the host to elicit approval, and requires servers to enforce authorization independently. The public tool-call documentation describes no signed per-call permission token that Spliff could validate. MCP form elicitation supplies a protocol exchange with the connected client. A client or tunnel that omits this capability uses the existing local approval path. The installed ChatGPT tunnel forwarding path has to be validated in a live beta session. [OpenAI custom MCP server guide](https://developers.openai.com/api/docs/guides/custom-mcp-server) · [OpenAI tool annotations](https://developers.openai.com/plugins/reference) · [OpenAI server guide](https://developers.openai.com/plugins/build/mcp-server) · [MCP elicitation specification](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation)

## Manual beta check

1. Use a disposable indexed library and a separate private staging directory. Start the existing Spliff tunnel in download-enabled mode with that catalog and refresh its connection in ChatGPT.
2. Search for a fixture release, select a candidate, stage it, and wait for the transfer to finish. Ask ChatGPT to show both available actions and their destinations. Ask for the original and downloaded spectrograms for one track.
3. Choose Keep both, decline the host form and check the library. Repeat and accept. Check that the old files stayed in place and that the new edition and the extra track were indexed. Confirm that repeating the same plan reports the same transaction.
4. Use a second fixture for verified Replace. Set the originals destination to the hidden recovery folder, accept the exact paths, then use the transaction ID to roll it back. Check that the original was restored and the downloaded replacement remains at the reported recovery path.
5. Exercise a disconnected peer or paused transfer. Check the reported Nicotine+ state and use the alternative search with a fresh candidate selection. A paused transfer can be authorized for exact-row retry in Nicotine+.

The regression fixtures use generated audio and temporary directories. The live Buju Banton music files are outside the test scope. Spectrum plots and waveform evidence support review. Source authenticity, exact pressing identification, acoustic fingerprints, crash replay and client-side plot rendering are separate verification limits.
