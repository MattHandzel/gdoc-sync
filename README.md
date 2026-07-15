# gdoc-sync

Sync Markdown files with Google Docs from the command line.

Write in Markdown, share as a Google Doc, get comments back into your Markdown
— without leaving the terminal.

```bash
gdoc-sync create draft.md        # → new Google Doc, styled, shared, URL on your clipboard
gdoc-sync push  draft.md         # local edits → the same doc (id/URL/sharing preserved)
gdoc-sync pull  draft.md         # doc edits + reviewer comments → back into your Markdown
```

## Why

Google Docs is where collaborators live; Markdown + git is where your writing
lives. Existing tools do one-way conversion or plain content sync. gdoc-sync
also round-trips **comments** (they land in your Markdown as
[CriticMarkup](https://github.com/CriticMarkup/CriticMarkup-toolkit)
annotations, with replies, anchored to the quoted text) and applies
**opinionated styling** (font + color themes) so the doc you share doesn't
look like a raw import.

## Features

- **create** — Markdown → new Google Doc via pandoc (headings, lists, tables,
  code blocks, links survive). Title from your first `# H1`, YAML `title:`, or
  the filename. The URL is printed *and copied to your clipboard*
  (Wayland/X11/macOS), and the doc is shared **anyone-with-link-can-comment**
  by default (`--edit`, `--view`, `--private` to change; configurable default).
- **push** — replace the linked doc's content in place; doc id, URL, and
  sharing are untouched. Optimistic locking: if someone edited the doc since
  your last pull, you're warned before overwriting (`--yes` for scripts).
- **pull** — the doc back as clean Markdown, including **multi-tab documents**,
  with every unresolved comment (+ replies) embedded as
  `{>>Author: comment<<}` right after the text it anchors to. Your local YAML
  frontmatter is preserved.
- **Styling** — a font (default Garamond) and a color theme (default
  [Catppuccin](https://catppuccin.com) Latte; all four flavors ship) applied
  document-wide on every create/push: rainbow-by-level heading colors, themed
  links, page background, pageless layout. Also repairs the invisible table
  borders pandoc imports produce.
- **watch** — live mode: remote edits auto-pull, local saves auto-push, and if
  both sides changed the remote version lands in `<name>.conflict.md` instead
  of clobbering your work.
- **Comment actions from Markdown** — under a pulled comment, write
  `{>>reply: thanks, fixed<<}` or `{>>resolve<<}` and the next `push` posts the
  reply / resolves the thread in the doc. `{>>comment: needs a source<<}`
  anywhere creates a new doc-level comment quoting your line.
- **Images** — local images are embedded on create/push; on pull, doc images
  download to `<name>-assets/` next to your file.
- **status / diff** — every linked file at a glance (`--remote` flags drift,
  `--json` for scripts); `diff` shows remote vs local before you overwrite
  either.
- **share / export** — share with specific people (`--with
  alice@example.com:edit`) or flip link sharing; export to pdf/docx/odt/epub.
- **doctor** — one command that tells a new user exactly what's missing
  (pandoc, OAuth client, token, API reachability).
- **link / unlink / open / auth / config** — bind an existing doc to a file,
  one-time OAuth, inspect effective settings.
- **rainbow** — 🌈 make the first paragraph of any doc rainbow-colored. No
  further justification will be offered.

## Install

```bash
pipx install gdoc-sync        # or: uvx gdoc-sync --help
# Nix
nix run github:MattHandzel/gdoc-sync -- --help
```

Requires [pandoc](https://pandoc.org/installing.html) on your PATH (the Nix
package bundles it).

## Setup (one time)

Google requires you to bring your own (free) OAuth client — ~5 minutes:
**[docs/oauth-setup.md](docs/oauth-setup.md)**. Then:

```bash
gdoc-sync auth --client ~/Downloads/client_secret_*.json
```

## Usage

```bash
# New doc from markdown; URL lands on your clipboard, shared for commenting
gdoc-sync create meeting-notes.md
gdoc-sync create post.md --title "Draft v2" --edit --open
gdoc-sync create spec.md --private --font "EB Garamond" --theme catppuccin-mocha

# Round-trip
gdoc-sync push post.md                  # local → doc
gdoc-sync pull post.md                  # doc (+ comments) → local
gdoc-sync pull 'https://docs.google.com/document/d/<id>/edit' notes.md   # pull & link

# Link a doc that already exists
gdoc-sync link post.md 'https://docs.google.com/document/d/<id>/edit'

# What's linked, and did anything drift?
gdoc-sync status --remote
gdoc-sync diff post.md

# Live sync while collaborators edit
gdoc-sync watch post.md              # or: --all, --interval 10, --no-push

# Sharing and export
gdoc-sync create spec.md --share-with alice@example.com:edit
gdoc-sync share spec.md --with bob@example.com --anyone view
gdoc-sync export spec.md --format pdf

# Something not working?
gdoc-sync doctor
```

Comments arrive like this:

```markdown
The proposal hinges on the Q3 numbers{>>Maya Chen: source for these? | Matt: added below<<}.
```

Reply or resolve without leaving your editor — put a marker right after the
pulled comment and `push`:

```markdown
...Q3 numbers{>>Maya Chen: source for these?<<}{>>reply: added in the appendix<<}.
...intro paragraph{>>Sam: too long<<}{>>resolve: trimmed<<}.
And a brand-new note for the doc:{>>comment: should we cite the 2025 survey?<<}
```

All `{>>...<<}` markers are stripped automatically on push (they live in the
doc's comment threads, not in your prose).

## Configuration

`~/.config/gdoc-sync/config.yaml` (override with `--config` or
`$GDOC_SYNC_CONFIG`):

```yaml
defaults:
  font: Garamond            # any font name from the Google Docs font picker
  theme: catppuccin-latte   # catppuccin-{latte,frappe,macchiato,mocha} | none
  share: comment            # private | view | comment | edit
  clipboard: true

# Optional: keep sync state (file↔doc mappings) somewhere synced/versioned.
# Default: ~/.local/state/gdoc-sync/state.yaml
# state_file: ~/notes/.gdoc-sync-state.yaml
```

`gdoc-sync config` prints the effective settings and where they came from.

## Limitations (honest ones)

- **New anchored comments can't be created via the API.** Google's Drive API
  saves but ignores comment anchors on Google Docs
  ([issue 292610078](https://issuetracker.google.com/issues/292610078)) — no
  third-party tool can highlight-comment a text range. That's why
  `{>>comment: ...<<}` becomes a *doc-level* comment quoting your text, while
  replies and resolves (which the API does support) attach to the real thread.
- Push replaces the whole doc body (suggested-edits history in the doc doesn't
  survive a push; comments do).
- Pull is high-level Markdown: footnotes and deeply nested formatting are not
  round-trip-faithful yet, and `diff` compares that lossy representation.
- `watch` polls (default 30s); Drive's real push notifications need a public
  webhook, which a CLI doesn't have.

## Roadmap

- Custom user themes in config; theme gallery
- Service-account auth for CI; GitHub Action recipe
- Folder/batch sync with `.gdocsyncignore`
- Library API (`import gdoc_sync`)

## Development

```bash
nix develop        # or: pip install -e ".[dev]"
pytest -q
ruff check src tests
```

MIT © Matthew Handzel
