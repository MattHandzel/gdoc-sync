"""gdoc-sync command-line interface.

Google-API imports happen lazily inside each handler so `--help`, `config`,
and unit tests never require network-facing dependencies to be importable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import set_config_override


def _existing_file(value: str) -> Path:
    p = Path(value).expanduser().resolve()
    if not p.exists():
        raise argparse.ArgumentTypeError(f"file not found: {p}")
    if not p.is_file():
        raise argparse.ArgumentTypeError(f"not a file: {p}")
    return p


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gdoc-sync",
        description="Sync Markdown files with Google Docs — create, push, pull, "
                    "comment round-trip, and opinionated styling.",
    )
    parser.add_argument("--config", metavar="PATH",
                        help="config file (overrides $GDOC_SYNC_CONFIG and the XDG default)")
    parser.add_argument("--version", action="version", version=f"gdoc-sync {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="create a new Google Doc from a markdown file")
    p.add_argument("file", type=_existing_file)
    p.add_argument("--title", help="override auto-derived title (default: first H1, YAML title:, or filename)")
    p.add_argument("--font", help="font family (default: from config, else Garamond)")
    p.add_argument("--theme", help="color theme (default: from config, else catppuccin-latte; 'none' to disable)")
    share = p.add_mutually_exclusive_group()
    share.add_argument("--private", action="store_true", help="do not share")
    share.add_argument("--edit", action="store_true", help="anyone with link can edit")
    share.add_argument("--view", action="store_true", help="anyone with link can view")
    p.add_argument("--share-with", action="append", metavar="EMAIL[:ROLE]",
                   help="also share with a specific account (role: view|comment|edit, "
                        "default comment); repeatable")
    p.add_argument("--no-copy", action="store_true", help="do not copy the URL to the clipboard")
    p.add_argument("--no-mapping", action="store_true", help="do not save the local→doc mapping")
    p.add_argument("--open", action="store_true", help="open the created doc in the browser")

    p = sub.add_parser("push", help="push local markdown to its linked Google Doc")
    p.add_argument("file", type=_existing_file)
    p.add_argument("--yes", "-y", action="store_true",
                   help="overwrite the remote even if it changed since last pull")
    p.add_argument("--font")
    p.add_argument("--theme")

    p = sub.add_parser("pull", help="pull a Google Doc as markdown (with comments as CriticMarkup)")
    p.add_argument("target", help="a linked local file, or a doc URL/ID")
    p.add_argument("output", nargs="?", help="output file when target is a URL/ID (links it too)")
    p.add_argument("--json", action="store_true", dest="json_out",
                   help="emit machine-readable JSON on stdout (progress goes to stderr)")

    p = sub.add_parser("watch", help="live sync: auto-pull remote edits, auto-push local ones")
    p.add_argument("files", nargs="*", type=_existing_file,
                   help="linked files to watch (default with --all: every mapping)")
    p.add_argument("--all", action="store_true", help="watch every linked file")
    p.add_argument("--interval", type=int, default=30, metavar="SEC",
                   help="poll interval in seconds (default 30)")
    p.add_argument("--no-push", action="store_true",
                   help="pull remote changes only; never auto-push local edits")

    p = sub.add_parser("share", help="change sharing on a linked doc")
    p.add_argument("target", help="a linked local file, or a doc URL/ID")
    p.add_argument("--with", action="append", dest="with_", metavar="EMAIL[:ROLE]",
                   help="share with a specific account (role: view|comment|edit, "
                        "default comment); repeatable")
    p.add_argument("--anyone", choices=["view", "comment", "edit"],
                   help="anyone with the link gets this role")
    p.add_argument("--private", action="store_true", help="remove link sharing")

    p = sub.add_parser("diff", help="diff local markdown against the doc's remote content")
    p.add_argument("file", type=_existing_file)

    p = sub.add_parser("export", help="export the doc via Drive (pdf, docx, odt, txt, html, epub)")
    p.add_argument("target", help="a linked local file, or a doc URL/ID")
    p.add_argument("--format", default="pdf", dest="fmt",
                   choices=["pdf", "docx", "odt", "txt", "html", "epub"])
    p.add_argument("-o", "--output", help="output path (default: <name>.<ext>)")

    p = sub.add_parser("open", help="open the linked doc in the browser")
    p.add_argument("target", help="a linked local file, or a doc URL/ID")

    p = sub.add_parser("link", help="link a local file to an existing Google Doc")
    p.add_argument("file", type=_existing_file)
    p.add_argument("url", help="Google Doc URL or ID")

    p = sub.add_parser("unlink", help="remove a file's local→doc mapping (doc is untouched)")
    p.add_argument("file", type=_existing_file)

    p = sub.add_parser("status", help="list linked files; --remote checks for drift")
    p.add_argument("--remote", action="store_true", help="query Google for remote changes")
    p.add_argument("--json", action="store_true", dest="json_out")

    p = sub.add_parser("auth", help="run the OAuth flow")
    p.add_argument("--client", metavar="PATH",
                   help="install this downloaded OAuth client-secret JSON first")
    p.add_argument("--force", action="store_true", help="discard the cached token and re-consent")

    sub.add_parser("config", help="print effective config, paths, and settings")

    p = sub.add_parser("doctor", help="diagnose the setup (pandoc, config, auth, API)")
    p.add_argument("--offline", action="store_true", help="skip the live API check")

    p = sub.add_parser("rainbow", help="🌈 color the first paragraph of a doc (easter egg)")
    p.add_argument("args", nargs=argparse.REMAINDER)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.config:
        set_config_override(args.config)

    try:
        _dispatch(args)
    except KeyboardInterrupt:
        sys.exit(130)


def _dispatch(args: argparse.Namespace) -> None:
    if args.command == "create":
        from .create import create_doc
        if args.private:
            share_mode = "private"
        elif args.edit:
            share_mode = "edit"
        elif args.view:
            share_mode = "view"
        else:
            from .config import get_share_default
            share_mode = get_share_default()
        _api_guard(lambda: create_doc(
            args.file,
            title=args.title,
            font=args.font,
            theme=args.theme,
            share_mode=share_mode,
            share_with=args.share_with,
            copy=False if args.no_copy else None,
            save_mapping=not args.no_mapping,
            open_in_browser=args.open,
        ))

    elif args.command == "push":
        from .push import push
        _api_guard(lambda: push(args.file, yes=args.yes, font=args.font, theme=args.theme))

    elif args.command == "pull":
        from .config import extract_doc_id_from_url, get_doc_id
        from .pull import pull
        target = Path(args.target).expanduser()
        if target.exists() and target.is_file():
            doc_id = get_doc_id(str(target.resolve()))
            if not doc_id:
                print(f"No Google Doc linked to {target}. Pass a doc URL/ID instead.",
                      file=sys.stderr)
                sys.exit(1)
            _api_guard(lambda: pull(doc_id, target.resolve(), json_out=args.json_out))
        else:
            doc_id = extract_doc_id_from_url(args.target)
            output = Path(args.output).expanduser().resolve() if args.output else None
            _api_guard(lambda: pull(doc_id, output, json_out=args.json_out))

    elif args.command == "watch":
        from .config import all_mappings
        from .watch import watch
        if args.all:
            files = [Path(f) for f in all_mappings() if Path(f).exists()]
        else:
            files = list(args.files)
        if not files:
            print("Nothing to watch — pass files or --all.", file=sys.stderr)
            sys.exit(1)
        _api_guard(lambda: watch(files, interval=args.interval, no_push=args.no_push))

    elif args.command == "share":
        from .extras import resolve_doc_id
        from .share import share
        if not (args.with_ or args.anyone or args.private):
            print("Nothing to do — pass --with, --anyone, or --private.", file=sys.stderr)
            sys.exit(1)
        doc_id, _ = resolve_doc_id(args.target)
        _api_guard(lambda: share(doc_id, with_=args.with_, anyone=args.anyone,
                                 private=args.private))

    elif args.command == "diff":
        from .extras import diff
        _api_guard(lambda: diff(args.file))

    elif args.command == "export":
        from .extras import export
        out = Path(args.output).expanduser().resolve() if args.output else None
        _api_guard(lambda: export(args.target, fmt=args.fmt, output=out))

    elif args.command == "open":
        from .extras import open_doc
        open_doc(args.target)

    elif args.command == "unlink":
        from .extras import unlink
        unlink(args.file)

    elif args.command == "link":
        from .config import extract_doc_id_from_url, set_doc_id
        doc_id = extract_doc_id_from_url(args.url)
        set_doc_id(str(args.file), doc_id)
        print(f"Linked {args.file} → {doc_id}")

    elif args.command == "status":
        from .status import status
        _api_guard(lambda: status(remote=args.remote, json_out=args.json_out))

    elif args.command == "auth":
        from .auth import run_auth
        _api_guard(lambda: run_auth(client=args.client, force=args.force))

    elif args.command == "config":
        _print_config()

    elif args.command == "doctor":
        from .doctor import doctor
        sys.exit(doctor(online=not args.offline))

    elif args.command == "rainbow":
        from .rainbow import main as rainbow_main
        rainbow_main(args.args)


def _print_config() -> None:
    from .config import (
        config_path,
        get_clipboard_default,
        get_font,
        get_share_default,
        get_theme,
        state_path,
    )
    from .style import available_themes
    cp = config_path()
    print(f"Config file: {cp}" + ("" if cp.exists() else "  (not created yet — defaults in effect)"))
    print(f"State file:  {state_path()}")
    print(f"  font:      {get_font()}")
    print(f"  theme:     {get_theme() or 'none'}  (available: {', '.join(available_themes())}, none)")
    print(f"  share:     {get_share_default()}")
    print(f"  clipboard: {get_clipboard_default()}")


def _api_guard(fn) -> None:
    """Run an API-touching action with friendly error reporting."""
    from googleapiclient.errors import HttpError
    try:
        fn()
    except HttpError as e:
        print(f"Google API error: {e}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"{e}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as e:
        print(f"{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
