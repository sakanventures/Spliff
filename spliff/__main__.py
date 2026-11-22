import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import mcp, web
from .catalog import initialize
from .review import protect, stage
from .nicotine import install_plugin, request
from .scanner import scan

def main():
    parser = argparse.ArgumentParser(prog="Spliff")
    parser.add_argument("--db", type=Path,
                        default=Path.home() / "Library/Application Support/Spliff/catalog.sqlite")
    commands = parser.add_subparsers(dest="command", required=True)
    scan_parser = commands.add_parser("scan", help="Index a music folder read-only")
    scan_parser.add_argument("root", type=Path)
    ui = commands.add_parser("serve", help="Open the local read-only dashboard")
    ui.add_argument("--port", type=int, default=8765)
    mcp_parser = commands.add_parser("mcp", help="Expose read-only MCP tools over stdio")
    mcp_parser.add_argument("--search-only", action="store_true",
                            help="Expose only Nicotine+ search to a connected chat")
    installer = commands.add_parser("install-nicotine-plugin", help="Install Spliff Search for Nicotine+")
    installer.add_argument("--plugins-dir", type=Path,
                           default=Path.home() / ".local/share/nicotine/plugins")
    commands.add_parser("nicotine-status", help="Check whether the Nicotine+ bridge is available")
    for action in ("protect", "unprotect"):
        commands.add_parser(action).add_argument("id", type=int)
    staged = commands.add_parser("stage", help="Record a lossless comparison for manual review")
    staged.add_argument("id", type=int)
    staged.add_argument("candidate", type=Path)

    args = parser.parse_args()
    db = args.db.expanduser().resolve()
    try:
        if args.command == "scan":
            root = args.root.expanduser()
            if not root.is_dir():
                raise ValueError("The music folder is unavailable")
            print(json.dumps(scan(db, root, lambda counts: print(counts, file=sys.stderr))))
            return 0
        if args.command == "install-nicotine-plugin":
            print(install_plugin(args.plugins_dir))
            return 0
        if args.command == "nicotine-status":
            print(json.dumps(request("status")))
            return 0
        if args.command == "mcp" and args.search_only:
            mcp.serve(None, search_only=True)
            return 0
        initialize(db)
        if args.command == "serve":
            web.serve(db, args.port)
        elif args.command == "mcp":
            mcp.serve(db)
        elif args.command == "stage":
            print(f"Staged review #{stage(db, args.id, args.candidate.expanduser())}")
        else:
            protect(db, args.id, args.command == "protect")
    except (ValueError, OSError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print(f"Spliff: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0

if __name__ == "__main__":
    sys.exit(main())
