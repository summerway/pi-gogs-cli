---
name: gogs-cli
description: Reusable, agent-agnostic Gogs CLI mirroring the GitHub CLI (`gh`) command surface. Repos, issues (list/view/create/edit/close/reopen/comment, plus gh-style `issue develop` to branch from an issue), and labels (read-only) over the Gogs API; pull requests (Gogs exposes no PR API) via headless Web-UI form POSTs — list/create/merge/close/reopen, no browser needed. Every webform write is verified through the API before success is reported. Base URL comes from config with optional GOGS_FALLBACK_URLS probing, so the same invocation works from several networks. Use for anything read/write against a Gogs instance. Cross-platform (macOS/Linux/containers), Python 3 stdlib, stateless; no workflow mapping.
---

# gogs-cli

A single command line tool for Gogs capability, usable by any agent (Codex,
Claude Code, pi, ...) or by a human, on desktop or headless/container hosts.
Mirrors the GitHub CLI (`gh`) command surface.

- **Interface contract:** `references/gogs-api.md` is the authoritative endpoint
  map, sourced from `https://gogs.io/api-reference/introduction`. It is the sole
  reference for the command set; update it when the API or the instance changes.
- **Encapsulation:** callers see only clean `gh`-style verbs. Whether an
  operation is served by the Gogs API or by Web-UI form POSTs is hidden.
- **Stateless:** no workflow mapping, no local database, no `LI/LPR` ids.

## Setup

`gogs-cli` is on PATH (e.g. `~/.local/bin/gogs-cli`, a symlink to this skill's
`scripts/gogs-cli`). Python 3 stdlib only — no dependencies.

Config (loaded from disk, never printed):
- `~/.config/gogs-cli/config` → `GOGS_TOKEN` (required for API ops).
  `~/.codex/local/gogs-workflow/.env` is honored as a legacy location.
- `<repo-root>/.gogs.local.env` → `GOGS_BASE_URL`, `GOGS_WEB_BASE_URL`,
  `GOGS_USERNAME`, `GOGS_PASSWORD` (web creds used only for webform PR ops).
- Either file may hold any of these keys; repo-level wins. On headless/container
  hosts put everything in the global file, with `GOGS_BASE_URL` pointing at the
  internal address (e.g. `http://gogs:3000` over a shared Docker network).

Default `OWNER/REPO` is derived from `git remote get-url origin`; pass an explicit
`OWNER/REPO` positional to override.

**Base resolution.** The Gogs web/API base is resolved at first use and cached
for the process: the configured `GOGS_BASE_URL`/`GOGS_WEB_BASE_URL` is tried
first, then each address in `GOGS_FALLBACK_URLS` (space- or comma-separated) in
order — the first that responds is used. Set e.g.
`GOGS_FALLBACK_URLS="http://192.168.1.10:10015 https://gogs.example.com"` so the
same invocation works on-LAN (fast, direct) and off-LAN (public host) with no
config change. There is no built-in default; with nothing configured the CLI
exits with setup instructions.

## Commands

```
# repo — API, read-only
gogs-cli repo view  [OWNER/REPO] [--json]
gogs-cli repo list  [--user USER] [--limit N] [--json]

# issue — API
gogs-cli issue list   [OWNER/REPO] [--state open|closed|all] [--label NAME...] [--assignee USER] [--limit N] [--json]
gogs-cli issue view   [OWNER/REPO] NUMBER [--json]
gogs-cli issue create --title T [--body B | -F FILE] [--assignee USER] [--label NAME...] [--milestone N] [OWNER/REPO] [--json]
gogs-cli issue edit   [OWNER/REPO] NUMBER [--title T] [--body B | -F FILE] [--state open|closed] [--json]
gogs-cli issue close  [OWNER/REPO] NUMBER [--json]
gogs-cli issue reopen [OWNER/REPO] NUMBER [--json]
gogs-cli issue comment [OWNER/REPO] NUMBER [--body B | -F FILE] [--json]
gogs-cli issue develop [OWNER/REPO] NUMBER [--base B] [--name NAME] [--json]   # local branch off --base, named from the issue (gh-style)

# label — API
gogs-cli label list [OWNER/REPO] [--json]

# pr — view via API; list/create/merge/close/reopen via Web UI (no PR API)
gogs-cli pr list   [OWNER/REPO] [--state open|closed|all] [--limit N] [--json]   # Web UI scrape
gogs-cli pr view   [OWNER/REPO] NUMBER [--json]                                   # API
gogs-cli pr create [--base B] [--head H] --title T [--body B | -F FILE] [OWNER/REPO] [--json]  # --base default develop, --head default current branch
gogs-cli pr merge  [OWNER/REPO] NUMBER [--merge|--squash|--rebase] [--delete-branch] [--json]
gogs-cli pr close  [OWNER/REPO] NUMBER [--json]
gogs-cli pr reopen [OWNER/REPO] NUMBER [--json]
```

`--json` switches any command to machine-readable JSON output (the raw API
object) so other tools/agents can compose. `--label NAME` is resolved to label
IDs via the labels API (Gogs accepts IDs only) — unknown name → error.

Exit codes: 0 success · 1 failure · 2 soft warning — `pr merge` that cannot
proceed (empty diff, conflict, already merged) exits 2 with a structured
`reason` instead of failing hard (details below). `--delete-branch` is
accepted for `gh` compatibility but **not automated**: it prints a reminder;
delete the branch manually.

## How PR ops work (no PR API exists)

Gogs exposes **no** pull-request API (`/pulls` returns 404), and the issues list
**excludes** PRs. So PR operations submit the Gogs Web UI's own forms directly —
**webform**: Python-stdlib session login + CSRF form POSTs, no browser and no OS
dependency, identical behavior on macOS, Linux, and containers:

- `pr list` scrapes `/pulls?type=all&state=…` (paginated)
- `pr create` POSTs the compare-page form (`title`/`content`); the redirect
  target carries the new PR number
- `pr merge` POSTs the `/pulls/N/merge` form (`merge_style`)
- `pr close/reopen` POST the comment form's hidden `status` field
  (`close`/`reopen`)
- Web login uses `GOGS_USERNAME`/`GOGS_PASSWORD` from the env files

For every write, `gogs-cli` **verifies the real remote state through the issues
API** (e.g. `pull_request.merged === true`, or issue `state`) before reporting
success — a submitted form alone is never reported as success.

`pr merge` that **cannot proceed** (empty-diff / nothing-to-merge, conflict, or
already-merged) is reported as a **non-zero warning (exit 2)**, not a hard
failure: Gogs renders no merge form in these cases, so `gogs-cli` reads the
page banner plus the issues-API `merged` flag and emits a structured `reason`
(`empty_diff` · `conflict` · `already_merged` · `not_mergeable`) with Gogs's raw
banner text — in both human and `--json` output. Genuine failures (auth error,
submitted-but-not-merged, or `#N` is not a PR) still exit 1.

If this Gogs offers only one merge style (e.g. `create_merge_commit` only),
`--squash`/`--rebase` fall back to the offered style with a stderr note.

Limitations: the `/pulls` closed tab groups merged PRs as "closed" — use
`pr view N` for the precise merged state; `--base/--head` are not supported by
`pr list`.

## Token safety

If `GOGS_TOKEN` is missing, `gogs-cli` stops and prints setup instructions — it
never guesses or retries. Tokens and `.gogs.local.env` contents are never
printed. Do not commit credentials or workflow mapping files.

## Importing as a module

`gogs_cli.py` is importable. Functions like `resolve_target`, `api`, `api_root`,
`create`/`view` helpers, and the `webform_*` PR functions can be
reused directly to avoid duplicating the API client.
