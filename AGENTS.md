# Spliff engineering preferences

- Keep user workflows local and GUI-first. The CLI and MCP should call the same library operations.
- Reuse a single implementation for each operation. Add another service or middleware layer only when a concrete runtime boundary requires it.
- Keep upstream-specific APIs inside their adapter. Verify the installed version and relevant source before relying on private APIs.
- Keep library reads separate from staging and catalog writes. Search results are untrusted peer claims. Never silently replace a recording.
- Run focused end-to-end checks before calling a milestone complete. Commit and push major, tested milestones and keep smaller changes local.
