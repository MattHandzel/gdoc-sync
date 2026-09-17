"""gdoc-sync command-line interface.

Google-API imports happen lazily inside each handler so `--help`, `config`,
and unit tests never require network-facing dependencies to be importable.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import DEFAULT_FONT, DEFAULT_THEME, set_config_override


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
                    "multi-tab documents, comment round-trip, and opinionated "
                    "styling.",
    )
    parser.add_argument("--config", metavar="PATH",
                        help="config file (overrides $GDOC_SYNC_CONFIG and the XDG default)")
    parser.add_argument("--version", action="version", version=f"gdoc-sync {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create", help="create a new Google Doc from a markdown file")
    p.add_argument("file", type=_existing_file)
    p.add_argument("--title", help="override auto-derived title (default: first H1, YAML title:, or filename)")
    p.add_argument("--font", help=f"font family (default: from config, else {DEFAULT_FONT})")
    p.add_argument("--theme", help=f"color theme (default: from config, else {DEFAULT_THEME}; "
                                   f"'none' to disable). `gdoc-sync config` lists them")
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

    p = sub.add_parser("import", help="create a new markdown file from an existing Google Doc")
    p.add_argument("url", help="Google Doc URL or ID")
    p.add_argument("-o", "--output", metavar="FILE",
                   help="write here (default: <doc-title-slug>.md inside --dest)")
    p.add_argument("--dest", metavar="DIR",
                   help="directory for the derived filename (default: config import_dir, else cwd)")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.add_argument("--no-frontmatter", action="store_true",
                   help="do not add the title/source/imported YAML header")
    oneway = p.add_mutually_exclusive_group()
    oneway.add_argument("--pull-only", action="store_true",
                        help="never push local edits back")
    oneway.add_argument("--two-way", action="store_true",
                        help="clear a previous --pull-only mark (the default)")
    p.add_argument("--open", action="store_true", help="open the doc in the browser")

    p = sub.add_parser("push", help="push local markdown to its linked Google Doc")
    p.add_argument("file", type=_existing_file)
    p.add_argument("--yes", "-y", action="store_true",
                   help="overwrite the remote even if it changed since last pull")
    p.add_argument("--font", help=f"font family (default: from config, else {DEFAULT_FONT})")
    p.add_argument("--theme", help=f"color theme (default: from config, else {DEFAULT_THEME}; "
                                   f"'none' to disable). `gdoc-sync config` lists them")
    p.add_argument("--prune-tabs", action="store_true",
                   help="delete tabs the markdown no longer has a "
                        "`# [TAB] <title>` section for (default: leave them)")
    p.add_argument("--flatten", action="store_true",
                   help="push a file with no [TAB] headers into a tabbed doc, "
                        "collapsing every tab into the first one")

    p = sub.add_parser("pull", help="pull a Google Doc as markdown (with comments as CriticMarkup)")
    p.add_argument("target", help="a linked local file, or a doc URL/ID")
    p.add_argument("output", nargs="?", help="output file when target is a URL/ID (links it too)")
    p.add_argument("--json", action="store_true", dest="json_out",
                   help="emit machine-readable JSON on stdout (progress goes to stderr)")

    p = sub.add_parser("watch", help="live sync: merge remote and local edits continuously")
    p.add_argument("files", nargs="*", type=_existing_file,
                   help="linked files to watch (default with --all: every mapping)")
    p.add_argument("--all", action="store_true", help="watch every linked file")
    p.add_argument("--interval", type=int, default=None, metavar="SEC",
                   help="poll interval in seconds (default: config watch_interval, else 15)")
    p.add_argument("--no-push", action="store_true",
                   help="pull remote changes only; never auto-push local edits")
    p.add_argument("--force", action="store_true",
                   help="bypass the guard that refuses to push an emptied file")
    p.add_argument("--json", action="store_true", dest="json_lines",
                   help="emit one JSON object per event (for editor integrations)")

    p = sub.add_parser("sync", help="reconcile a file with its doc once (safe two-way merge)")
    p.add_argument("files", nargs="*", type=_existing_file,
                   help="linked files to sync (default with --all: every mapping)")
    p.add_argument("--all", action="store_true", help="sync every linked file")
    p.add_argument("--no-push", action="store_true",
                   help="merge remote changes in, but never push local edits")
    p.add_argument("--force", action="store_true",
                   help="bypass the guard that refuses to push an emptied file")
    adopt = p.add_mutually_exclusive_group()
    adopt.add_argument("--adopt-local", action="store_true",
                       help="resolve by pushing the local file over the doc")
    adopt.add_argument("--adopt-remote", action="store_true",
                       help="resolve by overwriting the local file with the doc")
    p.add_argument("--json", action="store_true", dest="json_lines",
                   help="emit one JSON object per file")

    p = sub.add_parser("resolve", help="mark a conflicted file as resolved and resume syncing")
    p.add_argument("files", nargs="*", type=_existing_file,
                   help="conflicted files (default: list them)")
    p.add_argument("--all", action="store_true", help="resolve every conflicted file")

    p = sub.add_parser("restore", help="restore a synced file from its automatic backups")
    p.add_argument("file", type=_existing_file)
    p.add_argument("--index", type=int, default=None, metavar="N",
                   help="restore the Nth backup (0 = newest); omit to list them")

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
    oneway = p.add_mutually_exclusive_group()
    oneway.add_argument("--pull-only", action="store_true",
                        help="sync brings doc edits down but never pushes local edits up")
    oneway.add_argument("--two-way", action="store_true",
                        help="clear a previous --pull-only mark")

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

    # The command is the easter egg; its help was not meant to be. `args` used
    # to be an argparse.REMAINDER catch-all, so `rainbow --help` printed a
    # parser describing a single positional called "args" and nothing real.
    p = sub.add_parser("rainbow", help="🌈 color the first paragraph of a doc (easter egg)",
                       description="Color alternating characters (or words) of the first "
                                   "paragraph of a Google Doc in rainbow order.")
    p.add_argument("doc", help="a Google Doc URL or ID")
    p.add_argument("--tab", metavar="TAB_ID",
                   help="tab id, with or without the URL's 't.' prefix "
                        "(default: the first tab with content)")
    p.add_argument("--words", action="store_true",
                   help="cycle colors per word instead of per character")
    p.add_argument("--dry-run", action="store_true",
                   help="print what would change; do not apply it")

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.config:
        # An explicitly-named config that is not there is a mistake, not a
        # request for defaults — a typo'd --config used to run silently with
        # every setting (font, theme, share) reverted to the built-in default.
        override = Path(args.config).expanduser()
        if not override.exists():
            print(f"Config file not found: {override}", file=sys.stderr)
            sys.exit(2)
        set_config_override(override)

    try:
        _dispatch(args)
    except KeyboardInterrupt:
        sys.exit(130)


def _check_theme(theme: str | None) -> None:
    """Reject an unknown theme name before any API work happens.

    Covers both ``--theme`` and the config's ``theme:`` (which is what a
    ``--theme``-less create/push falls back to). :func:`_api_guard` catches the
    same error for the paths that reach a theme lookup later.
    """
    from .config import get_theme
    from .style import UnknownThemeError, check_theme
    try:
        check_theme(theme if theme is not None else get_theme())
    except UnknownThemeError as e:
        print(e, file=sys.stderr)
        sys.exit(2)


def _dispatch(args: argparse.Namespace) -> None:
    if args.command == "create":
        from .create import create_doc
        _check_theme(args.theme)
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

    elif args.command == "import":
        from .config import get_import_dir
        from .importer import import_doc
        dest = Path(args.dest).expanduser() if args.dest else get_import_dir()
        output = Path(args.output).expanduser() if args.output else None
        pull_only = True if args.pull_only else (False if args.two_way else None)

        def run_import():
            try:
                import_doc(args.url, output=output, dest=dest, force=args.force,
                           pull_only=pull_only, frontmatter=not args.no_frontmatter,
                           open_in_browser=args.open)
            except FileExistsError as e:
                print(e, file=sys.stderr)
                sys.exit(1)

        _api_guard(run_import)

    elif args.command == "push":
        from .push import push
        _check_theme(args.theme)
        _api_guard(lambda: push(args.file, yes=args.yes, font=args.font,
                                theme=args.theme, prune_tabs=args.prune_tabs,
                                flatten=args.flatten))

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
        from .config import get_watch_interval
        from .watch import watch
        files = _sync_targets(args, "watch")
        interval = args.interval if args.interval is not None else get_watch_interval()
        _api_guard(lambda: watch(files, interval=interval, no_push=args.no_push,
                                 force=args.force, json_lines=args.json_lines))

    elif args.command == "sync":
        files = _sync_targets(args, "sync")
        adopt = "local" if args.adopt_local else "remote" if args.adopt_remote else None
        _api_guard(lambda: _run_sync(files, adopt=adopt, no_push=args.no_push,
                                     force=args.force, json_lines=args.json_lines))

    elif args.command == "resolve":
        _cmd_resolve(args)

    elif args.command == "restore":
        _cmd_restore(args)

    elif args.command == "share":
        from .extras import resolve_doc_id
        from .share import share
        # Resolve the target first: a missing file used to be reported as
        # "pass --with, --anyone, or --private", blaming the flags for a
        # filename that was simply wrong.
        doc_id, _ = resolve_doc_id(args.target)
        if not (args.with_ or args.anyone or args.private):
            print("Nothing to do — pass --with, --anyone, or --private.", file=sys.stderr)
            sys.exit(1)
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
        from .config import set_doc_id, set_pull_only, validate_doc_id
        try:
            doc_id = validate_doc_id(args.url)
        except ValueError as e:
            print(str(e), file=sys.stderr)
            sys.exit(2)
        # Deliberately offline, and deliberately storing no revision: `link`
        # has not seen the doc, so it cannot honestly claim a baseline. The
        # push guard reads that absence as "never synced" and stops there
        # rather than replacing a document nobody has looked at.
        set_doc_id(str(args.file), doc_id)
        if args.pull_only or args.two_way:
            set_pull_only(str(args.file), args.pull_only)
        suffix = " (pull-only)" if args.pull_only else ""
        print(f"Linked {args.file} → {doc_id}{suffix}")

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
        rainbow_argv = [args.doc]
        if args.tab:
            rainbow_argv += ["--tab", args.tab]
        if args.words:
            rainbow_argv.append("--words")
        if args.dry_run:
            rainbow_argv.append("--dry-run")
        rainbow_main(rainbow_argv)


def _sync_targets(args, verb: str) -> list[Path]:
    """The files a sync/watch command should operate on.

    A mapping whose file has been renamed or deleted is skipped — but it is
    said out loud (on stderr, so ``--json`` output stays machine-clean).
    Silently dropping it is how a renamed note stops syncing without anyone
    noticing until the doc and the file have drifted apart for weeks.
    """
    from .config import all_mappings
    if args.all:
        files = []
        for mapped in all_mappings():
            path = Path(mapped)
            if path.exists():
                files.append(path)
            else:
                print(f"Warning: {path} is linked to a Google Doc but no longer "
                      f"exists — not {verb}ing it. If you renamed or deleted it: "
                      f"gdoc-sync unlink {path}", file=sys.stderr)
    else:
        files = list(args.files)
    if not files:
        print(f"Nothing to {verb} — pass files or --all.", file=sys.stderr)
        sys.exit(1)
    return files


def _run_sync(files, *, adopt, no_push, force, json_lines) -> None:
    """One reconcile pass over ``files``; exits non-zero if any conflicted."""
    import json as _json

    from .config import get_doc_id, is_pull_only, set_revision
    from .pull import render_doc
    from .push import push as push_file
    from .sync import reconcile

    conflicts = 0
    for path in files:
        doc_id = get_doc_id(str(path))
        if not doc_id:
            print(f"Skipping {path}: not linked to a Google Doc", file=sys.stderr)
            continue

        # `--adopt-local` on a one-way file is a contradiction: it asks for the
        # exact push the mark exists to prevent. Refuse it rather than guessing
        # — silently adopting *remote* instead would overwrite the local file,
        # which is the opposite of what was asked for.
        one_way = is_pull_only(str(path))
        if one_way and adopt == "local":
            print(f"Skipping {path}: marked pull-only, so --adopt-local would "
                  f"push it. Run `gdoc-sync link {path} {doc_id} --two-way` first "
                  f"if that is really what you want.", file=sys.stderr)
            continue

        try:
            outcome = reconcile(
                path, doc_id,
                render=lambda p, _d=doc_id: render_doc(_d, asset_path=p),
                push=lambda p, *, expected_fingerprint=None: push_file(
                    p, yes=True, merged=True,
                    expected_fingerprint=expected_fingerprint),
                allow_push=not (no_push or one_way),
                adopt=adopt,
                force=force,
                say=(lambda *a: None) if json_lines else print,
            )
        except Exception:
            # A crash in one document's reconcile aborts the whole `--all`
            # batch, and every frame in the traceback belongs to a library —
            # nothing in it says which of ~50 files was being synced. Pinning
            # the 2026-09-16 IndexError to a file took three crashed runs and a
            # vault-wide grep. Name the file, then let the traceback through
            # untouched.
            print(f"ERROR while syncing {path} (doc {doc_id})",
                  file=sys.stderr, flush=True)
            raise
        if outcome.revision:
            set_revision(str(path), outcome.revision)
        if outcome.conflicted:
            conflicts += 1

        if json_lines:
            print(_json.dumps({
                "event": outcome.action, "file": str(path), "detail": outcome.detail,
                "reload": outcome.wrote_local, "pushed": outcome.pushed,
                "conflict": outcome.conflicted, "backup": outcome.backup,
            }))
        else:
            print(f"{path.name}: {outcome.detail}")
            if outcome.backup:
                print(f"  backup: {outcome.backup}")

    if conflicts:
        sys.exit(2)


def _cmd_resolve(args) -> None:
    from .merge import has_conflict_markers
    from .syncstate import all_conflicts, clear_conflict

    conflicts = all_conflicts()
    if args.all:
        targets = [Path(p) for p in conflicts]
    elif args.files:
        targets = list(args.files)
    else:
        if not conflicts:
            print("No conflicted files.")
            return
        print("Conflicted files:")
        for p, c in conflicts.items():
            print(f"  {p}\n    since {c.since}: {c.detail}")
        print("\nResolve the file, then: gdoc-sync resolve <file>")
        return

    if not targets:
        print("No conflicted files.")
        return

    for path in targets:
        # Clearing the flag while markers are still in the text would push the
        # markers straight into the doc.
        try:
            if has_conflict_markers(path.read_text(encoding="utf-8")):
                print(f"{path.name}: still contains merge markers — "
                      f"remove them first.", file=sys.stderr)
                sys.exit(1)
        except OSError:
            pass
        if clear_conflict(path):
            print(f"{path.name}: resolved — syncing resumes.")
        else:
            print(f"{path.name}: was not marked conflicted.")


def _cmd_restore(args) -> None:
    from .config import atomic_write
    from .syncstate import backup_file, list_backups

    backups = list_backups(args.file)
    if not backups:
        print(f"No backups recorded for {args.file.name}.")
        return

    if args.index is None:
        print(f"Backups for {args.file.name} (newest first):")
        for i, b in enumerate(backups):
            size = b.stat().st_size
            print(f"  [{i}] {b.name}  ({size} bytes)")
        print(f"\nRestore with: gdoc-sync restore {args.file} --index 0")
        return

    if not 0 <= args.index < len(backups):
        print(f"No backup at index {args.index} (have 0..{len(backups) - 1}).",
              file=sys.stderr)
        sys.exit(1)

    chosen = backups[args.index]
    # Restoring is itself an overwrite, so the current contents get a backup too.
    backup_file(args.file, tag="pre-restore")
    atomic_write(args.file, chosen.read_text(encoding="utf-8"))
    print(f"Restored {args.file.name} from {chosen.name}.")
    print("The doc is untouched — run `gdoc-sync sync` when you're happy with it.")


def _print_config() -> None:
    from .config import (
        config_path,
        get_clipboard_default,
        get_conflict_style,
        get_font,
        get_share_default,
        get_theme,
        get_watch_interval,
        state_path,
    )
    from .style import available_themes
    from .syncstate import all_conflicts, sync_dir
    cp = config_path()
    print(f"Config file: {cp}" + ("" if cp.exists() else "  (not created yet — defaults in effect)"))
    print(f"State file:  {state_path()}")
    print(f"Sync data:   {sync_dir()}  (baselines + backups)")
    print(f"  font:           {get_font()}")
    print(f"  theme:          {get_theme() or 'none'}  (available: {', '.join(available_themes())}, none)")
    print(f"  share:          {get_share_default()}")
    print(f"  clipboard:      {get_clipboard_default()}")
    print(f"  conflict_style: {get_conflict_style()}")
    print(f"  watch_interval: {get_watch_interval()}s")
    conflicts = all_conflicts()
    if conflicts:
        print(f"\n  {len(conflicts)} unresolved conflict(s):")
        for p in conflicts:
            print(f"    {p}")


def _api_guard(fn) -> None:
    """Run an API-touching action with friendly error reporting."""
    from googleapiclient.errors import HttpError

    from .style import UnknownThemeError
    try:
        fn()
    except UnknownThemeError as e:
        # Reached when the theme comes from the config on a path with no
        # --theme flag to pre-check (sync, watch).
        print(e, file=sys.stderr)
        sys.exit(2)
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
