# Spliff engineering preferences

- Keep music processing on the user's machine. The dashboard, CLI, and MCP should call the same library operations.
- Make ChatGPT the primary interface for the album upgrade workflow. Keep the local dashboard as an alternate interface and use the same library operations from both.
- In ChatGPT, support a user-selected local file or folder, catalog checks, Nicotine+ candidate search when an existing FLAC or WAV is unavailable, and automatic staging and review when the user starts a folder sync. Keep manual candidate selection in Advanced.
- Reuse a single implementation for each operation. Add another service or middleware layer only when a concrete runtime boundary requires it.
- Keep upstream-specific APIs inside their adapter. Verify the installed version and relevant source before relying on private APIs.
- Keep library reads separate from staging and catalog writes. Search results are untrusted peer claims. Never silently replace a recording.
- After a completed album download, ask the user to choose **Keep both** or **Replace**. Show both reviewed plans and exact destinations before the user chooses. Their choice authorizes that plan. If the reviewed files, preference, or destinations change, require a fresh choice.
- Replace follows the library setting after verifying and indexing the replacement. Mac Trash is the default, and users can choose a hidden recovery folder. Keep both keeps the original files in the library.
- File downloaded releases as `<artist>/<category>/[YYYY] <release title>`. Look for existing artist folders under verified current and former names before proposing a new one. Use the downloaded edition's release year. Treat a deluxe reissue as an album and reserve Posthumous Albums for albums first released after the artist's death.
- File multi-disc audio releases in `Disc 1`, `Disc 2`, and subsequent disc folders. Name each file `01 Title.flac` (retaining its actual extension), restarting track numbers at 01 for each disc. Keep track numbers separate from the title tags.
- Keep separate artwork files at the top level of the release folder, outside disc folders.
- Run focused end-to-end checks before calling a milestone complete. Commit and push major, tested milestones and keep smaller changes local.
