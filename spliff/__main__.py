import argparse
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from . import mcp, sync, web
from .catalog import initialize
from .discogs_auth import setup_discogs
from .review import protect, stage
from .nicotine import install_plugin, request, transfer_control
from .scanner import scan
from .spectral import analyze_track
from .artist import library as artist_library, upgrades as artist_upgrades

def main():
    parser = argparse.ArgumentParser(prog="Spliff")
    parser.add_argument("--db", type=Path,
                        default=Path.home() / "Library/Application Support/Spliff/catalog.sqlite")
    commands = parser.add_subparsers(dest="command", required=True)
    services_parser = commands.add_parser("services", help="Manage background Mac services")
    services_parser.add_argument("arguments", nargs=argparse.REMAINDER)
    scan_parser = commands.add_parser("scan", help="Index a music folder read-only")
    scan_parser.add_argument("root", type=Path)
    commands.add_parser("sync", help="Scan a folder and schedule lossless upgrades").add_argument("folder")
    spectrum = commands.add_parser("spectrum", help="Analyze an indexed track locally")
    spectrum.add_argument("id", type=int)
    for command in ("artist-library", "artist-upgrades"):
        artist = commands.add_parser(command, help="Inspect owned artist releases" if command == "artist-library"
                                     else "Search for upgrades of owned artist releases")
        artist.add_argument("artist")
        artist.add_argument("--after", type=int, default=0)
        artist.add_argument("--limit", type=int, default=3 if command == "artist-upgrades" else 50)
    ui = commands.add_parser("serve", help="Open the local read-only dashboard")
    ui.add_argument("--port", type=int, default=8765)
    mcp_parser = commands.add_parser("mcp", help="Expose Spliff tools over stdio")
    mcp_parser.add_argument("--search-only", action="store_true",
                            help="Expose only Nicotine+ search to a connected chat")
    mcp_parser.add_argument("--allow-downloads", action="store_true",
                            help="Expose folder sync, downloads and upgrade review")
    mcp_parser.add_argument("--allow-discogs", action="store_true",
                            help="Expose read-only Discogs release lookups")
    mcp_parser.add_argument("--dashboard-port", type=int, default=8765,
                            help="Port of the local dashboard used for upgrade approval")
    installer = commands.add_parser("install-nicotine-plugin", help="Install Spliff Search for Nicotine+")
    installer.add_argument("--upgrade", action="store_true", help="Update an existing Spliff Search plugin")
    installer.add_argument("--plugins-dir", type=Path,
                           default=Path.home() / ".local/share/nicotine/plugins")
    control = commands.add_parser("nicotine-downloads", help="Enable or stop Spliff transfers locally")
    control.add_argument("action", choices=("enable", "stop"))
    commands.add_parser("nicotine-status", help="Check whether the Nicotine+ bridge is available")
    commands.add_parser("discogs-setup", help="Open a one-time local Discogs setup page")
    for action in ("protect", "unprotect"):
        commands.add_parser(action).add_argument("id", type=int)
    staged = commands.add_parser("stage", help="Record a lossless comparison for manual review")
    staged.add_argument("id", type=int)
    staged.add_argument("candidate", type=Path)

    args = parser.parse_args()
    db = args.db.expanduser().resolve()
    try:
        if args.command == "services":
            from .services import main as services_main
            print(json.dumps(services_main(db, args.arguments), indent=2))
            return 0
        if args.command == "discogs-setup":
            setup_discogs()
            return 0
        if args.command == "sync":
            print(json.dumps(sync.start(db, args.folder)))
            return 0
        if args.command == "scan":
            root = args.root.expanduser()
            if not root.is_dir():
                raise ValueError("The music folder is unavailable")
            print(json.dumps(scan(db, root, lambda counts: print(counts, file=sys.stderr))))
            return 0
        if args.command == "install-nicotine-plugin":
            print(install_plugin(args.plugins_dir, upgrade=args.upgrade))
            return 0
        if args.command == "nicotine-downloads":
            print(json.dumps(transfer_control(args.action)))
            return 0
        if args.command == "nicotine-status":
            print(json.dumps(request("status")))
            return 0
        if args.command == "mcp" and args.search_only and not args.allow_downloads:
            mcp.serve(None, search_only=True, allow_discogs=args.allow_discogs)
            return 0
        initialize(db)
        if args.command == "serve":
            web.serve(db, args.port)
        elif args.command == "spectrum":
            print(json.dumps(analyze_track(db, args.id)))
        elif args.command in ("artist-library", "artist-upgrades"):
            operation = artist_library if args.command == "artist-library" else artist_upgrades
            print(json.dumps(operation(db, args.artist, args.after, args.limit)))
        elif args.command == "mcp":
            mcp.serve(db, search_only=args.search_only, allow_downloads=args.allow_downloads,
                      allow_discogs=args.allow_discogs, dashboard_port=args.dashboard_port)
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
