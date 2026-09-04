# Changelog

## Unreleased

### Data safety: what a persona audit found, and what is closed

Nine reviewers with different jobs — a tech writer, a vault user, an academic,
a Docs-side PM, a CI engineer, an IT-security admin, a QA lead, a first-run
developer and a Markdown pedant — audited the tool offline and found sixteen
ways to lose data or touch other people's. Eleven are closed here; the rest
(anchor-preserving pushes, suggestions, default sharing, OAuth scope) need
design decisions and are tracked separately.

- **A collaborator's display name could execute an action.** A pulled comment
  from a user named `resolve` rendered as `{>>resolve: ...<<}`, which the next
  push ran against the nearest preceding thread — somebody else's. Names that
  parse as actions are now quoted, and nothing a doc contains can become one.
- **Comment actions posted again on every push.** Markers were sent to Drive
  but never removed from the file, so three pushes of one file left three
  identical replies. Applied markers are now consumed (the file is backed up
  and rewritten before the upload); failed ones stay.
- **Any local file could be uploaded to Drive world-readable** by typing
  `![](../../.ssh/id_rsa)` into a shared doc. Image targets are now contained
  to the note's project, hidden directories are refused, and only real image
  bytes are staged. Staging files are tagged and stale ones reaped.
- **A doc that rendered empty or truncated emptied the note**, as a clean
  `merged`. A render under half the previous size is now a conflict.
- **A note truncated to one line replaced the whole doc.** A push that would
  shrink the doc to under 20% of its last synced size is refused without
  `--force`.
- **An edit typed into the doc during a merged push vanished.** The engine now
  hands the push a text fingerprint of the doc it merged; a mismatch aborts
  the upload and the pass is retried, with both edits intact. A push that
  fails for any reason no longer leaves the local edit unpushed forever.
- **`link` then `push` replaced a document you had never seen, with no
  prompt**, because a missing stored revision bypassed the drift check. It is
  now a hard stop: pull or diff first, or pass `--yes`. `link` also validates
  the URL or id instead of accepting any string, and `create` records the new
  doc's revision.
- **Parallel `gdoc-sync` processes lost mappings.** Twenty concurrent `link`
  calls used to leave thirteen; the state file is now written under an
  advisory lock (`state.yaml.lock`) and every read-modify-write holds it.
- **Footnotes were deleted on pull, then from the doc on push.** They now come
  back as `[^n]` references and definitions, numbered in reference order, and
  the pull warns if any went missing.
- **Equations swapped bodies when a paragraph moved in Docs.** Equal counts
  used to be matched by position; every equation is now matched to its
  surrounding prose, and an unmatched one is left as a placeholder and
  counted rather than guessed.
- **A tabbed push cleared the tab before it was rewritten, with no copy.**
  Each tab is snapshotted into the backup directory first; on failure the
  backup path, the pre-push revision and the version-history link are
  printed.
- **A section could be written into the wrong tab** when one tab could not be
  created. Sections are now paired to their tab ids explicitly.
- Every push warns how many anchored comments will lose their anchor in the
  Docs UI (they re-attach on the next pull).

### Wrong input is now an error instead of a quiet wrong result

Every one of these used to exit 0 and do something plausible-looking:

- `gdoc-sync open nonsense` treated the typo as a document id and opened
  `docs.google.com/document/d/nonsense` in the browser. Anything that is
  neither an existing linked file nor a Docs URL/id now exits 2 and opens
  nothing. `share`, `diff` and `export` share the check, so
  `share <missing file>` names the file instead of blaming `--with`/`--anyone`.
- `gdoc-sync auth --client typo.json` started the OAuth consent flow before
  discovering the file was not there. It now fails first.
- An unknown `--theme` (or `theme:` in the config) silently produced a
  completely unstyled document. It now exits 2 and lists the available themes.
  `none` still means "no theme".
- A `--config` path that does not exist was accepted, silently reverting every
  setting to its default. It now exits 2 naming the path. `$GDOC_SYNC_CONFIG`
  keeps its lenient behaviour.
- `sync --all` and `watch --all` skipped mappings whose file had been renamed
  or deleted without a word, so a renamed note just stopped syncing. Each one
  now prints a warning to stderr (stdout stays clean for `--json`) suggesting
  `gdoc-sync unlink <path>`, and `status` lists them together.
- `create --help` claimed the default theme was `catppuccin-latte`; it is
  `professional`, and the help text now comes from the constant. `rainbow
  --help` described a parser with one argument called `args`; it now documents
  `doc`, `--tab`, `--words` and `--dry-run`.

### Version metadata is checked, not assumed

`pyproject.toml` and `flake.nix` said 0.8.0 while the package said 0.9.0.
All three now say 0.9.0, and `scripts/check_version.py` (run in CI) fails with
a diff if they ever disagree again. The PyPI classifier is
`Development Status :: 3 - Alpha`, which is where the project actually is.

### Multi-tab documents, written as well as read

`pull` has always flattened a tabbed doc into one file with `# [TAB] <title>`
headers. `push` could not put them back: a push is a Drive docx import, which
replaces a *whole document*, so it would have moved every tab's content into
the first tab and dropped the rest. That is why `import` marked a tabbed doc
pull-only, and why there was no way to author one at all.

Now `create` and `push` read those headers. `create` makes a doc with one tab
per section, in order; `push` matches each section to a tab **by title** and
rewrites that tab in place, so the tab keeps its id and the links and comments
anchored in it survive. `## [TAB]` nests a child tab under its parent.

Tab content cannot go through pandoc's docx writer, because Drive's importer
cannot address a tab. It is compiled from pandoc's own AST into Docs API
requests instead (`mdrequests.py`), so both paths agree about what the markdown
means. Headings, bold, italic, strikethrough, inline code, links, nested
ordered and unordered lists, blockquotes, code blocks, tables, images and
callouts all work, and the font and colour theme are applied per tab. The API
has no request that creates a footnote or an equation inside a tab, so those
degrade visibly: footnotes are inlined in brackets, equations written as
LaTeX.

Two deliberate safety choices:

- A tab your file does not mention is **left alone**. Shared docs grow tabs
  nobody's notes know about. `push --prune-tabs` removes them when you ask.
- Pushing a file with **no** `[TAB]` headers into a doc that has several tabs
  is refused outright, with the fix printed. `--flatten` overrides it.

That refusal replaces the pull-only mark tabbed imports used to get: it tests
the file actually in hand at the moment of the push, rather than a flag
recorded once at import time. `--pull-only` still exists for documents that
genuinely cannot round-trip.

### Strikethrough survives a pull

`push` has always written `~~struck~~` into the doc; the puller had no case
for it, so every pull quietly deleted the markers and the file churned on the
next sync.

### Code blocks keep their font when a theme is applied

Applying the document-wide font used to sweep over monospace runs. Since the
monospace font is the *only* surviving signal that a paragraph was a fenced
code block, that quietly turned every fence into prose on the next pull. The
font pass now re-asserts monospace the same way it already re-asserted bold.

### Images sync both ways now

Add an image in the Google Doc and the next `pull`/`sync`/`watch` downloads
it beside your file (`<name>-assets/`, or one global `image_dir:` from the
config) and writes the link into your Markdown. Images your file already
references are recognised **by content**: after every push recreates the
doc's image objects, each one hashes back to the file it came from, keeps
its path and alt text, and is never re-downloaded or renamed.

This also fixes a churn bug: the sync baseline used to be recorded without
image links while every later render had them, so any doc with images
reported a remote edit that never happened — and merges could duplicate
image references. Baselines now render through the same image resolver as
every other render.

## 0.9.0 (2026-08-05)

### Start from a doc you didn't write

Every path into gdoc-sync assumed the Markdown existed first. `create` makes a
doc from a file; `push` and `pull` move a link that already exists. The case
that had no answer was the common one — somebody shares a doc with you and you
want it in your notes.

`pull <url> <file>` came closest, but it made you invent the filename and left
the result with no record of where the text came from.

```bash
gdoc-sync import https://docs.google.com/document/d/<id>/edit --dest ~/notes
```

- **Named after the document.** `Matt x Tzu 🌻 1-1 advisory` becomes
  `matt-x-tzu-1-1-advisory.md`. Emoji, accents and punctuation are reduced to
  something you can type and wiki-link; `-o FILE` if you'd rather choose.
- **Provenance in frontmatter** — `title`, `source`, `gdoc_id`, `imported` —
  written through the YAML dumper, so a title full of colons and quotes still
  produces a header that parses. `pull` already preserves frontmatter, so it
  survives every later sync. `--no-frontmatter` opts out.
- **Linked and reconciled on arrival.** The mapping *and* the merge ancestors
  are registered, so the first `sync`/`watch` tick sees an up-to-date file
  rather than a file it has no history for and has to ask about.
- **`--dest DIR`**, or set `import_dir:` in the config to send every import to
  one notes folder.
- Refuses to overwrite an existing file, and says how to link that file
  instead. `--force` to overwrite anyway.

### Tabbed docs are no longer silently flattened

`pull` reads every tab; `push` writes a single body. Pushing a pulled tabbed
doc therefore moved the whole flattened document — `# [TAB]` headers and all —
into the *first* tab and deleted the others. The advice was "don't round-trip a
multi-tab doc", which `watch --all` cannot follow on your behalf.

Files can now be marked **pull-only**, and `import` marks a multi-tab doc that
way automatically:

- `sync` and `watch` honour the mark per file: doc edits come down, local edits
  are never pushed up. `watch`'s start banner counts them.
- `--adopt-local` on a pull-only file is refused rather than reinterpreted —
  it asks for exactly the push the mark prevents, and silently adopting
  *remote* instead would overwrite the local file.
- `status` labels them; `gdoc-sync link <file> <url> --pull-only` / `--two-way`
  sets and clears the mark; `unlink` clears it.

## 0.8.0 (2026-08-03)

### Callouts become coloured panels

`> [!NOTE]` blocks reached the doc as *prose*. pandoc's reader recognises
GitHub's five alert types and turns them into a `Div` — and pandoc's docx
writer then flattens that `Div` completely, so the word "Note" arrived as an
ordinary paragraph indistinguishable from the text around it. Obsidian-only
types never got that far: `> [!TLDR]` was not an alert to pandoc at all, and
arrived as the literal characters `[!TLDR]`. Neither is visible as a mistake
in the markdown, only in the document you already shared.

They are now panels: a tinted background, an accent rule down the left edge,
and an icon beside the title.

- **Every type GitHub and Obsidian define**, and all of Obsidian's aliases —
  25 spellings across 14 rendered kinds. Where the two disagree (Obsidian
  treats `important` as a synonym of `tip`, `caution` of `warning`) GitHub
  wins and all five stay distinct.
- **Colours are derived from the theme**, not tabulated: the tint is the
  accent blended into the page, and on a dark page the accent is lifted first.
  A user-defined theme from `themes:` gets callouts that match it without
  anyone maintaining fourteen colours per theme.
- **Custom titles** (`> [!TIP] Try this first`) round trip in both directions.
- **Your own spelling survives a pull.** `[!INFO]` and `[!NOTE]` are the same
  panel, and a fold marker (`[!NOTE]-`) has no meaning in a Google Doc, so
  both are restored from the local file rather than rewritten every sync.
- A callout inside a code fence stays code; `> [!SOMETHINGELSE]` stays an
  ordinary blockquote.

Each callout is a one-row, one-column table — the only structure the Docs API
can give an exact *extent* to. Blockquote paragraphs arrive with 24pt indents,
but a list inside one arrives with a bullet's indents and no end indent at
all, so anything reading the extent off indentation ends the callout at its
first bullet. Verified end to end on a document carrying all 14 kinds plus a
custom title, an alias with a fold marker, a nested list, bold, code and math:
16 panels rendered, and the pulled markdown came back byte-identical.

### Fence languages now survive a merge, not just a pull

`restore_fence_languages` ran in `pull` only, so ```` ```python ```` was
restored in the file while the merge ancestor and the doc render both still
said ```` ``` ````. Both sides then differed from the base on that line, and a
remote edit next to a fence stopped `watch` with a conflict to resolve by
hand. It now runs inside `render_doc`, alongside the equation restoration that
was moved there in 0.7.0 for exactly the same reason — and for the same
reason, this retires spurious conflicts rather than fixing lost data.

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
