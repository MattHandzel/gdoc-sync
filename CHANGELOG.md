# Changelog

## 0.7.1 (2026-08-02)

### Fewer round trips

Every measurement below is a median of interleaved before/after runs against
the real API, on a 26 KB document.

- **`create` and `push`: 288 ms faster (780 → 491 ms of styling).** Applying
  the theme and applying table borders each fetched the whole document and then
  sent its own batch — four sequential round trips over the same document, for
  two operations whose requests can simply be concatenated. They now share one
  fetch and one batch. Safe because none of these requests move text: character
  styling, table-cell styling and document style all leave every index where it
  was. If the combined batch fails, it falls back to the two separate calls, so
  a single malformed table cannot also cost the document its styling.
- **Every `pull`, `sync` and `watch` tick: 75 ms faster (446 → 371 ms).** The
  document body and its comments are independent requests that were made one
  after the other; they now overlap, and the markdown conversion runs while the
  comments are still in flight.

Two things deliberately left alone, having been measured rather than assumed:
CLI startup (34 ms, with the Google client libraries already imported lazily),
and the Neovim statusline component (2.3 us per redraw).

## 0.7.0 (2026-08-02)

### Pull no longer deletes your equations

Pushing LaTeX always worked — pandoc turns `$x$` and `$$...$$` into OMML and
Drive imports that as a *native* Google Docs equation. Pulling destroyed it.
The Docs API returns an equation as `{"equation": {}}` and nothing else: no
LaTeX, no MathML, not even a length. The converter had no text to emit, so
every formula was silently deleted on the way back: `$I$ — count of insights`
came back as `— count of insights`.

`pull` and `--adopt-remote` overwrite the file with that render, so both lost
equations outright and without a word. `watch` did not — the three-way merge
compares each side against its own snapshot, and an ancestor rendered just as
lossily cancels the deletion out. What the watcher did instead was raise a
spurious *conflict* whenever anyone edited a line an equation sat on.

Equations now survive:

- Each is restored from the local file, the only remaining copy of the source.
  A round trip is lossless even when reviewers rewrite the prose around the
  math, reflow paragraphs, or comment throughout.
- Restoration aligns paragraph by paragraph rather than on a whole-file count,
  so an equation added in Google Docs costs only its own paragraph instead of
  blanking every formula in the document.
- Whatever genuinely cannot be matched becomes a visible `` `[equation]` ``
  marker plus a warning carrying the count — never a silent deletion.
- This happens inside `render_doc`, so `sync`, `watch` and `--adopt-remote` are
  covered too, not only `pull` — which also retires the spurious conflicts,
  because the ancestor and the doc now agree with the file about the math.
- LaTeX pandoc cannot parse (`$m^$` — a superscript with no argument) is now
  reported at push time. Pandoc warns, writes it into the doc as literal text
  and exits 0, so previously the only evidence was a formula-shaped hole in a
  shared document.

Verified end to end against a real 26 KB document carrying 128 equations: 126
imported as native Google Docs equations, the 2 malformed ones reported, and
all 128 spans byte-identical after a full create → pull round trip.

## 0.6.0 (2026-07-26)

### Two-way sync no longer loses edits

`watch` in 0.5.x could destroy work. Editing the same document in Google Docs
and in markdown, then touching either side again, could wipe one of them. Three
separate faults combined to cause it:

- **Change detection was guesswork.** A local mtime and the doc's `revisionId`
  stood in for "did this change?". Google rewrites `revisionId` on autosave and
  presence changes, so a doc merely *open* in a browser tab looked like an
  endless stream of remote edits — each one overwriting the local file with a
  lossy re-render of the doc.
- **Conflicts were forgotten instantly.** On detecting that both sides had
  changed, `watch` wrote a `.conflict.md` copy and then advanced *both*
  baselines — erasing the divergence from its own memory, so the next
  one-sided change overwrote the side that had never been merged.
- **There was nothing to merge against.** No ancestor was stored, so the only
  available moves were "overwrite local" or "overwrite remote".

The sync engine has been rebuilt around a real three-way merge:

- Every linked file now keeps **two snapshots** — the local file's bytes, and
  what the doc rendered to — as of the last successful sync. Each side is
  compared against its own snapshot, so the lossy `md → doc → md` round trip
  can no longer masquerade as an edit.
- Divergence is **merged**, not settled by fiat: edits in different parts of a
  document all survive. Only genuinely overlapping edits conflict.
- A conflict is **sticky**. It is recorded in the state file, survives a
  restart, and suspends automatic sync for that file until you resolve it.
- **Every write is backed up** (timestamped, in the state directory) and
  atomic; `gdoc-sync restore` brings any of them back.
- Guards refuse to push an emptied file over a doc with content, and abandon a
  merge whose file changed underneath it mid-sync.
- With no ancestor and a real divergence the engine **refuses to guess**,
  asking for `--adopt-local` or `--adopt-remote`.

New commands: `sync` (one safe reconcile), `resolve`, `restore`.
`watch --json` emits one event per line, so an editor knows exactly when a file
changed on disk and when a conflict was raised.

### Styling is now innate to the document

Headings imported as blue whatever the theme. pandoc's reference docx
hard-codes Word's accent blue into the heading *style definitions*, which
Google Docs imports as the document's named styles; recolouring runs through
the API painted over that without changing it, so the heading dropdown, the
outline, and every heading typed later in Google Docs stayed blue.

The theme is now baked into a generated reference docx, so the document's named
styles genuinely carry it. This also fixes **footnotes rendering in the default
font** — footnote text lives outside `body.content` and was unreachable from
the API restyling pass entirely. Code keeps its monospace face.

### Also

- Clipboard copy is platform-aware: Wayland, X11, macOS, `clip.exe` under WSL,
  Termux, and an OSC 52 escape so copying still works over plain SSH. A
  `clipboard_command:` setting overrides the detection.
- The state file is written atomically, and a corrupt one is moved aside rather
  than silently discarding every mapping.
- Sync baselines and backups always live in the XDG state directory, never
  beside a state file that happens to sit inside a synced notes vault.
- New settings: `conflict_style` (`markers`/`sidecar`), `watch_interval`,
  `clipboard_command`.
- Default watch interval is 15s (was 30s).
- `create` and a plain `push` now record the sync baseline. Both leave the
  file and the doc in agreement, which is the one free moment to capture a
  merge ancestor; without it the *first* `sync`/`watch` on a newly created doc
  saw two texts differing only by the lossy round trip and — correctly but
  uselessly — refused to guess which side to keep.

## 0.5.3 (2026-07-26)

- `watch` no longer sends desktop notifications. It is normally spawned by
  gdoc-sync.nvim for the file being edited, so a pull/push fired on nearly
  every tick — one notification per tick for an operation the user had just
  performed themselves. All events still print to stdout, which is where the
  editor surfaces them, so no information is lost.

## 0.5.2 (2026-07-17)

- `doctor` never opens a browser: a dead token now reports as a failure with
  the fix command and consent screen link instead of silently launching the
  interactive OAuth flow mid-diagnosis.

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
