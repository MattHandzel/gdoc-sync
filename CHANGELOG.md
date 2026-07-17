# Changelog

## 0.5.1 (2026-07-17)

Maximally helpful error messages.

- Every auth failure now prints the exact Cloud Console link to fix it, not
  just a description of what to do: the missing-client-secret error links the
  create-a-client page and both enable-API pages; a failed token refresh links
  the consent screen with your project preselected and explains the
  Testing-mode 7 day expiry.
- `gdoc-sync auth` prints the OAuth client's project id and the
  publish-to-Production link after authenticating.
- `gdoc-sync doctor` shows which project the client belongs to and the same
  consent screen link.

## 0.5.0 (2026-07-15)

Themes and polish.

- New default theme **`professional`**: paginated, near-black text, a navy
  heading ramp. The look a shared work doc is expected to have. Catppuccin
  (all four flavors) is still built in, plus a new `minimal` theme.
- **User-defined themes** in the config's `themes:` section: background, text,
  link, pageless, and headings as one color, a per-level list, or a map.
  `gdoc-sync config` lists every theme it can see.
- macOS: watch-mode notifications now fall back to `osascript`; install and
  platform notes in the README. CI already covers macOS.
- README: demo GIF (rendered with vhs against the real API, see
  `demo/render.sh`), a screenshot of a synced doc, and corrected install
  instructions (the package isn't on PyPI yet; install from GitHub).

## 0.4.0 (2026-07-15)

Live sync and comment actions.

- **`watch`**: poll linked files (or `--all`); remote edits auto-pull, local
  edits auto-push (`--no-push` to disable), and when both sides changed the
  remote version is written to `<name>.conflict.md` instead of clobbering.
  Best-effort desktop notifications via `notify-send`.
- **Comment actions from Markdown**: on `push`, `{>>reply: ...<<}` and
  `{>>resolve<<}` / `{>>resolve: ...<<}` placed after a pulled comment post a
  reply or resolve that thread; `{>>comment: ...<<}` anywhere creates a new
  doc-level comment quoting the preceding line. (Anchored comment *creation*
  remains impossible via Google's API; see README limitations.)

## 0.3.0 (2026-07-15)

Robustness and reach.

- **Images**: local images referenced in Markdown are embedded on
  create/push (pandoc resolves them relative to the file); on `pull`, doc
  images download to `<name>-assets/` and arrive as `![image](...)` links
- **Retries**: all Google API calls now retry with exponential backoff on
  429/5xx/rate-limit errors
- `pull --json` for a machine-readable result (progress moves to stderr)
- `create --share-with email[:view|comment|edit]` (repeatable) and a new
  **`share`** command (`--with`, `--anyone`, `--private`) for existing docs
- New commands: **`doctor`** (setup diagnostics), **`diff`** (local vs
  remote), **`export`** (pdf/docx/odt/txt/html/epub via Drive), **`open`**,
  and **`unlink`**

## 0.2.0 (2026-07-15)

First public release. Previously a personal vault script.

- Single `gdoc-sync` CLI (`create`, `push`, `pull`, `link`, `status`, `auth`,
  `config`, `rainbow`) replacing the bash + nix-shell wrapper
- XDG config (`~/.config/gdoc-sync/config.yaml`) with `--config` /
  `$GDOC_SYNC_CONFIG` override; settings split from machine-written state;
  legacy single-file `.gdoc-sync.yaml` format still honored
- Bring-your-own OAuth client flow (`auth --client`) with setup walkthrough
- `status` command with `--remote` drift check and `--json`
- Non-interactive push guard (`--yes`)
- Unit tests, CI (ubuntu/macos × 3.10/3.12), Nix flake (package + devShell)
