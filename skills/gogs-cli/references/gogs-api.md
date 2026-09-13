# Gogs API Reference (authoritative for `gogs-cli`)

Source of truth for the `gogs-cli` command surface. Captured from the official
Gogs API docs (`https://gogs.io/api-reference/introduction`, full index
`https://gogs.io/llms.txt`) and verified against the local instance
`http://192.168.100.3:10015` (HTTP status in parentheses, probed 2026-08-01).

Any future change to `gogs-cli` commands MUST be justified against this map.
The official API docs at `https://gogs.io/api-reference/introduction` are the
sole interface reference; update this file when the API or the instance changes.

## Conventions

- Base path: `/api/v1`.
- Auth: `Authorization: token <TOKEN>` (the token is read from
  `~/.codex/local/gogs-workflow/.env` as `GOGS_TOKEN`; never printed).
- `:owner/:repo` resolves from the `origin` git remote unless overridden.
- List endpoints paginate with `?page=N` (~10/page). Gogs ignores `limit`; the
  `type=` param (a Gitea feature) is also ignored. `state=all` is **not** honored
  — pass a concrete `state=open`/`closed` and combine them for "all".

## Repository (API — read only is wrapped)

| Method | Path | Status | `gogs-cli` |
|---|---|---|---|
| GET | `/repos/:owner/:repo` | 200 | `repo view` |
| GET | `/user/repos` | 200 | `repo list` (authenticated user) |
| GET | `/users/:username/repos` | 200 | `repo list --user <name>` |
| GET | `/repos/:owner/:repo/branches` | 200 | _(not wrapped; use `git`)_ |

`repo create/delete/fork/collaborators` are management endpoints — intentionally
**not** wrapped.

## Issues (API)

| Method | Path | Status | `gogs-cli` |
|---|---|---|---|
| GET | `/repos/:owner/:repo/issues` | 200 | `issue list` (supports `state`, `labels`, `assignee`) |
| GET | `/repos/:owner/:repo/issues/:number` | 200 | `issue view` |
| POST | `/repos/:owner/:repo/issues` | 201 | `issue create` (body: `title`, `body`, `assignee`, `milestone`, `labels` IDs) |
| PATCH | `/repos/:owner/:repo/issues/:number` | 200 | `issue edit` / `issue close` / `issue reopen` (body: `state`) |
| GET | `/repos/:owner/:repo/issues/:number/comments` | 200 | _(shown by `issue view`)_ |
| POST | `/repos/:owner/:repo/issues/:number/comments` | 201 | `issue comment` |

Notes:
- `labels` on create/edit takes label **IDs**, not names. `gogs-cli --label NAME`
  resolves names → IDs via `GET /repos/:owner/:repo/labels` (gh-compatible UX).
- Closing = `PATCH /issues/:number {"state": "closed"}`; reopen = `{"state": "open"}`.

## Labels (API)

| Method | Path | Status | `gogs-cli` |
|---|---|---|---|
| GET | `/repos/:owner/:repo/labels` | 200 | `label list` |

Only `label list` is wrapped (to support `--label` name resolution). Label
create/edit/delete are management ops — not wrapped.

## Pull Requests (NO API — ego black box)

| Path | Status | Conclusion |
|---|---|---|
| `GET /repos/:owner/:repo/pulls` | **404** | No PR list endpoint |
| `GET /repos/:owner/:repo/pulls/:number` | **404** | No PR get endpoint |
| `POST /repos/:owner/:repo/pulls` | **absent** | No PR create endpoint |
| merge/close/reopen | **absent** | No PR mutation endpoints |

Gogs registers **no PR API routes**, and the issues list **excludes** PRs
entirely (a PR is only observable via `GET /issues/:number`, which then carries a
`pull_request` key). Therefore in `gogs-cli`:

- **`pr view N`** — served by the **issues endpoint**: `GET /issues/N`, asserting
  `pull_request` is present. Authoritative (includes `merged`, `base`, `head`).
- **`pr list`** — **no API exists**; the issues list returns zero PRs. Served by
  **ego-browser scraping the Web UI**: `/pulls?type=all&state=<open|closed>
  &labels=0&milestone=0&assignee=0`. The ego profile auto-logs in via
  `GOGS_USERNAME`/`GOGS_PASSWORD` when it hits `/user/login`. Limitation: the
  closed tab groups merged PRs as "closed"; use `pr view N` for the precise
  merged state. `--base/--head` are not filterable here.
- **PR write** (`pr create`, `pr merge`, `pr close`, `pr reopen`) is performed by
  **ego-browser on the Gogs Web UI as a black box** (`/compare/...`, `/pulls/:n`,
  `/pulls/:n/merge`). After the browser action, `gogs-cli` **verifies the real
  state via the issues API** (e.g. `pull_request.merged === true`, or issue
  `state`) before reporting success — never trusts a click alone.
- **`pr merge` nothing-to-merge detection**: when Gogs renders no merge button
  (empty-diff / nothing-to-merge, conflict, or already-merged), `gogs-cli` reads
  the page banner + the issues-API `merged` flag and returns a **non-zero warning
  (exit 2)** carrying `reason` (`empty_diff` · `conflict` · `already_merged` ·
  `not_mergeable`) and the raw banner text — distinct from a hard failure (exit 1).

## Token setup

When `GOGS_TOKEN` is missing, `gogs-cli` stops and instructs:

1. Log in to the Gogs Web UI.
2. Open `<base>/user/settings/applications`.
3. Generate a token (e.g. named `gogs-cli`).
4. Save to `~/.codex/local/gogs-workflow/.env` as `GOGS_TOKEN=<token>` (chmod 600).
