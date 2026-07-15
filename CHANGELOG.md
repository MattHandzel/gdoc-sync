# Changelog

## 0.2.0 — 2026-07-15

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
