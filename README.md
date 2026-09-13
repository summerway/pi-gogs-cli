# pi-gogs-cli

A [pi coding agent](https://github.com/earendil-works/pi-coding-agent) skill that ships **gogs-cli** — a GitHub-CLI-style (`gh`) command line for self-hosted [Gogs](https://gogs.io) instances. Usable by any agent (pi, Codex, Claude Code, ...) or by a human, on desktop, headless boxes, containers, and CI. Python 3 stdlib only — zero dependencies.

## Why

Gogs has a usable API for repos/issues/labels but **no pull-request API at all** (`/pulls` 404s), and its issues list excludes PRs. Existing CLIs either ignore PRs or drive a real browser. gogs-cli gives an agent full repo/issue/PR capability with one stateless tool:

- **repo / issue / label** — plain Gogs API (`gh`-style verbs, `issue develop` branches off an issue gh-style)
- **pr list/create/merge/close/reopen** — headless **webform engine**: session login + CSRF form POSTs against the Gogs Web UI itself, no browser, no OS dependency
- every webform write is **verified through the API** before success is reported — a submitted form alone is never reported as success
- `--json` on every command; structured `reason` warnings (exit 2) for `pr merge` no-ops (already merged / empty diff / conflict)

## Install (pi)

```bash
pi install npm:pi-gogs-cli
```

The skill's `scripts/gogs-cli` lands under `~/.pi/agent/npm/pi-gogs-cli/...`; symlink it onto PATH:

```bash
ln -sf ~/.pi/agent/npm/pi-gogs-cli/skills/gogs-cli/scripts/gogs-cli ~/.local/bin/gogs-cli
```

Any other agent or a human can use the same checkout directly.

## Configuration

All config is loaded from disk, never printed.

| File | Keys |
|---|---|
| `~/.config/gogs-cli/config` | `GOGS_TOKEN` (required for API ops), `GOGS_USERNAME` / `GOGS_PASSWORD` (webform PR ops) |
| `~/.codex/local/gogs-workflow/.env` | legacy location, still honored |
| `<repo-root>/.gogs.local.env` | `GOGS_BASE_URL`, `GOGS_WEB_BASE_URL`, `GOGS_USERNAME`, `GOGS_PASSWORD` |

Repo-level wins. Create a token in Gogs under `user/settings/applications`.

**Base resolution:** `GOGS_BASE_URL` is probed live; then each address in `GOGS_FALLBACK_URLS` (space/comma-separated) in order — first responder wins, cached per process:

```bash
GOGS_FALLBACK_URLS="http://192.168.1.10:10015 https://gogs.example.com"
```

Same invocation on-LAN and off-LAN, no config change. No built-in default; unconfigured runs exit with setup instructions.

Default `OWNER/REPO` derives from `git remote get-url origin`.

## Command surface

```
gogs-cli repo  view|list
gogs-cli issue list|view|create|edit|close|reopen|comment|develop
gogs-cli label list
gogs-cli pr    list|view|create|merge|close|reopen
```

Full reference lives in the skill itself (`skills/gogs-cli/SKILL.md`) and is what agents see; the authoritative endpoint map is `skills/gogs-cli/references/gogs-api.md`.

## Merge styles

If the instance offers only one merge style (many Gogs setups expose `create_merge_commit` only), `--squash`/`--rebase` fall back to the offered style with a note. `--delete-branch` is accepted for `gh` compatibility but prints a reminder (manual delete).

## License

[MIT](LICENSE)
