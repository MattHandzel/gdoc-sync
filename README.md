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
- **status** — every linked file at a glance; `--remote` flags docs that
  changed since your last sync; `--json` for scripts.
- **link / auth / config** — bind an existing doc to a file, one-time OAuth,
  inspect effective settings.
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
```

Comments arrive like this:

```markdown
The proposal hinges on the Q3 numbers{>>Maya Chen: source for these? | Matt: added below<<}.
```

They're stripped automatically on the next `push` (they live in the doc, not
in your prose).

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
  third-party tool can highlight-comment a text range. Pulled comments are
  anchored by quoted text; pushing comment *replies/resolves* from Markdown is
  on the roadmap (that direction the API does support).
- Push replaces the whole doc body (suggested-edits history in the doc doesn't
  survive a push; comments do).
- Pull is high-level Markdown: images, footnotes, and deeply nested formatting
  are not round-trip-faithful yet.

## Roadmap

- `watch` — live mode: auto-pull on remote change, auto-push on save, conflict
  files instead of clobbering
- Reply to / resolve comments from Markdown markers
- Images on create/push and download-on-pull
- Share with specific emails; service-account auth for CI
- Custom user themes in config

## Development

```bash
nix develop        # or: pip install -e ".[dev]"
pytest -q
ruff check src tests
```

MIT © Matthew Handzel
