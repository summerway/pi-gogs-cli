#!/usr/bin/env python3
"""gogs-cli — reusable, agent-agnostic Gogs capability CLI.

gh-style command surface wrapping the Gogs API and (for pull requests, which
have no API) the Gogs Web UI. Stateless: no workflow mapping, no local database,
no LI/LPR ids. Any agent (Codex, Claude Code, pi, ...) calls the same `gogs-cli`
command or imports this module directly.

Command tree (mirrors `gh`):
  repo   view | list                         (API)
  issue  list | view | create | edit | close | reopen | comment | develop   (API; develop = local branch)
  label  list                                (API)
  pr     list                                (web UI scrape: no PR list API)
  pr     view                                (API: PR read via issues endpoint)
  pr     create | merge | close | reopen     (web UI write + API verify)

PR ops drive the Web UI via the webform engine: session-cookie login + CSRF
form POSTs, Python stdlib only — no browser, no OS dependency, runs anywhere
(desktop, container, CI).

Auth/config (loaded from disk, never printed):
  ~/.config/gogs-cli/config           -> GOGS_TOKEN (+ GOGS_USERNAME/GOGS_PASSWORD
                                          for webform PR ops)
  ~/.codex/local/gogs-workflow/.env   -> legacy location, still honored
  <repo-root>/.gogs.local.env         -> GOGS_BASE_URL, GOGS_WEB_BASE_URL,
                                         GOGS_USERNAME, GOGS_PASSWORD
  GOGS_FALLBACK_URLS env              -> extra web bases probed in order when
                                         the configured base is unreachable
Default :owner/:repo comes from `git remote get-url origin`.

Interface contract: references/gogs-api.md (authoritative endpoint map).
"""

from __future__ import annotations

import argparse
import functools
import http.cookiejar
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence
from urllib import error, request
from urllib.parse import quote, urlencode, urlparse

USER_AGENT = "gogs-cli/1.0"
ENV_GLOBAL_CANDIDATES = [
    Path("~/.config/gogs-cli/config").expanduser(),          # canonical
    Path("~/.codex/local/gogs-workflow/.env").expanduser(),   # legacy compat
]

# Optional extra web bases tried (in order) when the configured base is
# unreachable — useful when one instance is reachable via LAN at home and via
# a public host elsewhere. Same instance, several addresses. There is no
# built-in default: set GOGS_FALLBACK_URLS="http://lan-host:port https://public.host".


# --------------------------------------------------------------------------- #
# Config / env
# --------------------------------------------------------------------------- #
def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ[key] = value


def load_all_env(repo_root: Path | None) -> None:
    if repo_root is not None:
        load_env_file(repo_root / ".gogs.local.env")
    for candidate in ENV_GLOBAL_CANDIDATES:
        load_env_file(candidate)


def resolve_repo_root(path: str | None) -> Path:
    start = Path(path or os.getcwd()).expanduser().resolve()
    res = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start, check=False, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if res.returncode != 0:
        raise SystemExit(f"Not inside a git repository: {start}")
    return Path(res.stdout.strip()).resolve()


def run_git(repo_root: Path, args: Sequence[str]) -> str:
    res = subprocess.run(
        ["git", *args], cwd=repo_root, check=False, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if res.returncode != 0:
        detail = res.stderr.strip() or res.stdout.strip()
        raise SystemExit(f"git {' '.join(args)} failed: {detail}")
    return res.stdout.strip()


# --------------------------------------------------------------------------- #
# Target resolution
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Target:
    owner: str
    repo: str
    host: str
    web_url: str
    remote_url: str = ""
    repo_root: str = ""


def _probe_base(base: str, timeout: float = 2.0) -> bool:
    """True if the host responds at all; any HTTP status counts as reachable."""
    try:
        req = request.Request(
            base.rstrip("/") + "/api/v1/version",
            headers={"User-Agent": USER_AGENT}, method="GET",
        )
        with request.urlopen(req, timeout=timeout):
            return True
    except error.HTTPError:
        return True  # connected; the status code is irrelevant for a connectivity probe
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def resolved_web_base() -> str:
    """Live Gogs web base: configured value first, then GOGS_FALLBACK_URLS in
    order. Each candidate is probed; the first that responds wins. Cached per
    process."""
    configured = (os.environ.get("GOGS_WEB_BASE_URL") or os.environ.get("GOGS_BASE_URL") or "").strip().rstrip("/")
    if configured.endswith("/api/v1"):
        configured = configured[: -len("/api/v1")]
    chain: list[str] = []
    if configured:
        chain.append(configured)
    for b in (os.environ.get("GOGS_FALLBACK_URLS") or "").replace(",", " ").split():
        b = b.strip().rstrip("/")
        if b and b not in chain:
            chain.append(b)
    if not chain:
        raise SystemExit(
            "No Gogs base URL configured. Set GOGS_BASE_URL (or GOGS_WEB_BASE_URL)\n"
            f"in {ENV_GLOBAL_CANDIDATES[0]} or <repo-root>/.gogs.local.env, e.g.:\n"
            "  GOGS_BASE_URL=https://your-gogs.example.com\n"
            "Optional: GOGS_FALLBACK_URLS=\"http://lan-host:port https://public.host\"\n"
            "lets the same invocation work from several networks."
        )
    for b in chain:
        if _probe_base(b):
            return b
    return chain[0]  # nothing responded; let a later call fail with a real error


def configured_web_base(default: str) -> str:
    return resolved_web_base()


def _target_from_spec(repo_spec: str, repo_root: Path | None) -> Target:
    parts = repo_spec.strip().strip("/").split("/")
    if len(parts) < 2:
        raise SystemExit(f"Cannot parse OWNER/REPO from: {repo_spec}")
    owner, repo = parts[-2], re.sub(r"\.git$", "", parts[-1])
    load_all_env(repo_root)
    base = resolved_web_base()
    host = urlparse(base).netloc or "gogs"
    return Target(owner=owner, repo=repo, host=host, web_url=f"{base}/{owner}/{repo}")


def _target_from_remote(remote: str, repo_root_arg: str | None) -> Target:
    root = resolve_repo_root(repo_root_arg)
    load_all_env(root)
    remote_url = run_git(root, ["remote", "get-url", remote or "origin"])

    ssh = re.match(r"^(?:ssh://)?git@([^:/]+)(?::|/)(.+?)(?:\.git)?$", remote_url.strip())
    if ssh:
        host = ssh.group(1)
        p = ssh.group(2).strip("/").split("/")
        if len(p) < 2:
            raise SystemExit(f"Cannot parse owner/repo from remote: {remote_url}")
        owner, repo = p[-2], p[-1]
        web = f"{configured_web_base(f'https://{host}')}/{owner}/{repo}"
        return Target(owner, repo, host, web, remote_url, str(root))

    parsed = urlparse(remote_url.strip())
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        pp = parsed.path.strip("/").split("/")
        if len(pp) < 2:
            raise SystemExit(f"Cannot parse owner/repo from remote: {remote_url}")
        owner, repo = pp[-2], re.sub(r"\.git$", "", pp[-1])
        prefix = "/".join(pp[:-2])
        base = configured_web_base(f"{parsed.scheme}://{parsed.netloc}")
        web_path = "/".join(s for s in [prefix, owner, repo] if s)
        return Target(owner, repo, parsed.netloc, f"{base}/{web_path}", remote_url, str(root))

    raise SystemExit(f"Unsupported remote URL format: {remote_url}")


def resolve_target(repo_spec: str | None, remote: str, repo_root_arg: str | None) -> Target:
    if repo_spec and "/" in repo_spec and not repo_spec.startswith("http"):
        root: Path | None = resolve_repo_root(repo_root_arg) if repo_root_arg else None
        return _target_from_spec(repo_spec, root)
    return _target_from_remote(remote, repo_root_arg)


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #
def _request(
    url: str, method: str, path_for_msg: str,
    payload: dict[str, Any] | None = None,
    ok: tuple[int, ...] = (200,),
    token_required: bool = True,
) -> Any:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token_required:
        headers["Authorization"] = f"token {gogs_token()}"
    req = request.Request(url, data=data, method=method, headers=headers)
    try:
        with request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            if resp.status not in ok:
                raise SystemExit(f"{method} {path_for_msg} failed: HTTP {resp.status}: {body}")
            return json.loads(body) if body else {}
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise SystemExit(f"{method} {path_for_msg} failed: HTTP {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise SystemExit(f"{method} {path_for_msg} failed: {exc.reason}") from exc


def api(
    t: Target, method: str, path: str,
    payload: dict[str, Any] | None = None,
    ok: tuple[int, ...] = (200,), token_required: bool = True,
) -> Any:
    return _request(f"{api_base(t)}/{path.lstrip('/')}", method, path, payload, ok, token_required)


def _global_api_base() -> str:
    explicit = os.environ.get("GOGS_API_BASE_URL")
    if explicit:
        base = explicit.rstrip("/")
        return base if base.endswith("/api/v1") else f"{base}/api/v1"
    return resolved_web_base().rstrip("/") + "/api/v1"


def api_root(
    method: str, path: str,
    payload: dict[str, Any] | None = None,
    ok: tuple[int, ...] = (200,), token_required: bool = True,
) -> Any:
    return _request(f"{_global_api_base()}/{path.lstrip('/')}", method, path, payload, ok, token_required)


def api_base(t: Target) -> str:
    explicit = os.environ.get("GOGS_API_BASE_URL")
    if explicit:
        base = explicit.rstrip("/")
        return base if base.endswith("/api/v1") else f"{base}/api/v1"
    owner_repo = f"/{t.owner}/{t.repo}"
    if t.web_url.endswith(owner_repo):
        base = t.web_url[: -len(owner_repo)]
    else:
        base = resolved_web_base()
    return f"{base.rstrip('/')}/api/v1"


def token_setup_message() -> str:
    base = resolved_web_base()
    return "\n".join([
        "GOGS_TOKEN is not set. Gogs API operations require an access token.",
        "",
        "Create one in Gogs:",
        f"1. Log in and open: {base}/user/settings/applications",
        "2. Generate a token (for example named: gogs-cli)",
        f"3. Save it in: {ENV_GLOBAL}",
        "",
        "Example:",
        "mkdir -p ~/.config/gogs-cli",
        "printf 'GOGS_TOKEN=%s\\n' '<token>' >> ~/.config/gogs-cli/config",
        "chmod 600 ~/.config/gogs-cli/config",
    ])


def gogs_token() -> str:
    token = os.environ.get("GOGS_TOKEN", "").strip()
    if not token:
        raise SystemExit(token_setup_message())
    return token


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
def emit(obj: Any, json_mode: bool, human: Callable[[Any], None]) -> None:
    if json_mode:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    else:
        human(obj)


def read_text(body: str, body_file: str | None) -> str:
    if body_file:
        if body_file == "-":
            return sys.stdin.read()
        return Path(body_file).expanduser().read_text(encoding="utf-8")
    return body or ""


def _owner_repo(t: Target) -> str:
    return f"repos/{quote(t.owner)}/{quote(t.repo)}"


# --------------------------------------------------------------------------- #
# repo
# --------------------------------------------------------------------------- #
def repo_view(t: Target, json_mode: bool) -> None:
    emit(api(t, "GET", _owner_repo(t)), json_mode, _fmt_repo)


def repo_list(user: str | None, limit: int, json_mode: bool, remote: str, repo_root_arg: str | None) -> None:
    load_all_env(resolve_repo_root(repo_root_arg) if repo_root_arg else None)
    rows: list[dict[str, Any]] = []
    page = 1
    base_path = f"users/{quote(user)}/repos" if user else "user/repos"
    while len(rows) < limit and page <= 20:
        batch = api_root("GET", f"{base_path}?page={page}")
        if not batch:
            break
        rows.extend(batch)
        page += 1
    rows = rows[:limit]
    emit(rows, json_mode, _fmt_repo_list)


def _fmt_repo(r: dict[str, Any]) -> None:
    print(f"{r.get('full_name') or '/'.join(filter(None, [r.get('owner'), r.get('name')]))}")
    print(f"  url:    {r.get('html_url') or r.get('website') or ''}")
    print(f"  desc:   {(r.get('description') or '').strip() or '(none)'}")
    print(f"  default branch: {r.get('default_branch')}")
    print(f"  private: {bool(r.get('private'))}")


def _fmt_repo_list(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("(no repositories)")
        return
    for r in rows:
        print(f"{r.get('full_name') or r.get('name')}\t{(r.get('description') or '').strip()}")


# --------------------------------------------------------------------------- #
# label
# --------------------------------------------------------------------------- #
def label_list(t: Target, json_mode: bool) -> None:
    emit(api(t, "GET", f"{_owner_repo(t)}/labels"), json_mode, _fmt_label_list)


def resolve_label_ids(t: Target, names: list[str]) -> list[int]:
    if not names:
        return []
    labels = api(t, "GET", f"{_owner_repo(t)}/labels")
    by_name = {lab.get("name"): lab.get("id") for lab in labels}
    ids: list[int] = []
    for n in names:
        if n not in by_name:
            avail = ", ".join(sorted(str(k) for k in by_name if k)) or "(none)"
            raise SystemExit(f"Unknown label: {n}. Available: {avail}")
        ids.append(int(by_name[n]))
    return ids


def _fmt_label_list(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("(no labels)")
        return
    for lab in rows:
        print(f"{lab.get('id')}\t{lab.get('name')}\t{lab.get('color', '')}")


# --------------------------------------------------------------------------- #
# issue
# --------------------------------------------------------------------------- #
def _query(**params: Any) -> str:
    pairs = [(k, v) for k, v in params.items() if v not in (None, "", [])]
    if not pairs:
        return ""
    return "?" + "&".join(f"{k}={quote(str(v), safe=',')}" for k, v in pairs)


def _fetch_issues(t: Target, state: str, label_ids: list[int], assignee: str | None, cap: int) -> list[dict[str, Any]]:
    """Page the issues endpoint for one state. Gogs ignores `limit`/`type` and
    pages by `?page=` (~10/page); `state=all` is not honored, so callers pass a
    concrete state and combine open+closed."""
    q = _query(state=state, labels=",".join(str(i) for i in label_ids) or None, assignee=assignee)
    sep = "&" if q else "?"
    rows: list[dict[str, Any]] = []
    page = 1
    while len(rows) < cap and page <= 20:
        batch = api(t, "GET", f"{_owner_repo(t)}/issues{q}{sep}page={page}")
        if not batch:
            break
        rows.extend(batch)
        page += 1
    return rows


def issue_list(t: Target, state: str, labels: list[str], assignee: str | None, limit: int, json_mode: bool) -> None:
    label_ids = resolve_label_ids(t, labels)
    states = ["open", "closed"] if state == "all" else [state]
    rows: list[dict[str, Any]] = []
    for s in states:
        rows.extend(_fetch_issues(t, s, label_ids, assignee, max(limit - len(rows), 0) + 1))
        if len(rows) >= limit:
            break
    emit(rows[:limit], json_mode, _fmt_issue_list)


def issue_view(t: Target, number: int, json_mode: bool) -> None:
    emit(api(t, "GET", f"{_owner_repo(t)}/issues/{number}"), json_mode, _fmt_issue)


def issue_create(
    t: Target, title: str, body: str, assignee: str | None,
    milestone: int | None, label_names: list[str], closed: bool, json_mode: bool,
) -> None:
    payload: dict[str, Any] = {"title": title, "body": body}
    if assignee:
        payload["assignee"] = assignee
    if milestone is not None:
        payload["milestone"] = milestone
    ids = resolve_label_ids(t, label_names)
    if ids:
        payload["labels"] = ids
    if closed:
        payload["closed"] = True
    emit(api(t, "POST", f"{_owner_repo(t)}/issues", payload, ok=(200, 201)), json_mode, _fmt_issue)


def issue_edit(
    t: Target, number: int, title: str | None, body: str | None,
    state: str | None, json_mode: bool,
) -> None:
    payload: dict[str, Any] = {}
    if title:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    if state:
        payload["state"] = state
    if not payload:
        raise SystemExit("issue edit: nothing to change (give --title/--body/--state).")
    emit(api(t, "PATCH", f"{_owner_repo(t)}/issues/{number}", payload, ok=(200, 201)), json_mode, _fmt_issue)


def issue_set_state(t: Target, number: int, state: str, json_mode: bool) -> None:
    emit(api(t, "PATCH", f"{_owner_repo(t)}/issues/{number}", {"state": state}, ok=(200, 201)), json_mode, _fmt_issue)


def issue_comment(t: Target, number: int, body: str, json_mode: bool) -> None:
    if not body.strip():
        raise SystemExit("issue comment: --body (or -F) is required and must not be empty.")
    emit(api(t, "POST", f"{_owner_repo(t)}/issues/{number}/comments", {"body": body}, ok=(200, 201)), json_mode, _fmt_comment)


def _slugify(text: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-") or "issue")[:40]


def issue_develop(t: Target, number: int, base: str, name: str | None, json_mode: bool) -> None:
    """Create a local branch off <base>, named from the issue (gh `issue develop`)."""
    if not t.repo_root:
        raise SystemExit(
            "issue develop: needs the repo context — run it from inside the repo "
            "without an explicit OWNER/REPO (the branch is created locally from "
            "the origin remote's issue repo)."
        )
    root = Path(t.repo_root)
    data = api(t, "GET", f"{_owner_repo(t)}/issues/{number}")
    branch = name or f"{number}-{_slugify(data.get('title') or '')}"
    run_git(root, ["checkout", "-b", branch, base])
    obj = {"issue": number, "branch": branch, "base": base, "title": data.get("title") or ""}
    emit(obj, json_mode, lambda o: print(
        f"created branch '{o['branch']}' off '{o['base']}' for issue #{o['issue']}: {o['title']}"))


def _fmt_issue(i: dict[str, Any]) -> None:
    print(f"#{i.get('number')}  [{i.get('state')}]  {i.get('title')}")
    if i.get("html_url") or i.get("url"):
        print(f"  url: {i.get('html_url') or i.get('url')}")
    body = (i.get("body") or "").strip()
    if body:
        print("  ----")
        for line in body.splitlines()[:12]:
            print(f"  {line}")
        if len(body.splitlines()) > 12:
            print("  ...")


def _fmt_issue_list(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("(no issues)")
        return
    for i in rows:
        print(f"#{i.get('number')}\t[{i.get('state')}]\t{i.get('title')}")


def _fmt_comment(c: dict[str, Any]) -> None:
    print(f"comment #{c.get('id')} on issue #{c.get('issue_number')}")
    print(f"  url: {c.get('html_url') or c.get('url') or '(none)'}")
    print(f"  body: {(c.get('body') or '').strip()}")


# --------------------------------------------------------------------------- #
# pr (read)
#   view  -> GET /issues/:n (API; PRs are issues with a pull_request key)
#   list  -> web UI scrape (Gogs issues list EXCLUDES PRs; no PR list API)
# --------------------------------------------------------------------------- #
def pr_view(t: Target, number: int, json_mode: bool) -> None:
    data = api(t, "GET", f"{_owner_repo(t)}/issues/{number}")
    if not data.get("pull_request"):
        raise SystemExit(f"#{number} is an issue, not a pull request.")
    emit(data, json_mode, _fmt_pr)


def _fmt_pr(i: dict[str, Any]) -> None:
    pr = i.get("pull_request") or {}
    merged = "merged" if pr.get("merged") else i.get("state", "open")
    print(f"PR #{i.get('number')}  [{merged}]  {i.get('title')}")
    if i.get("html_url"):
        print(f"  url: {i.get('html_url')}")
    if pr.get("head") and pr.get("base"):
        print(f"  {pr['head'].get('ref')} -> {pr['base'].get('ref')}")


def _fmt_pr_list(rows: list[dict[str, Any]]) -> None:
    if not rows:
        print("(no pull requests)")
        return
    for i in rows:
        pr = i.get("pull_request") or {}
        state = "merged" if pr.get("merged") else i.get("state", "open")
        print(f"#{i.get('number')}\t[{state}]\t{i.get('title')}")


def _verify_pr_state(t: Target, number: int) -> dict[str, Any]:
    """Authoritative read-back via the issues API."""
    return api(t, "GET", f"{_owner_repo(t)}/issues/{number}")


# --------------------------------------------------------------------------- #
# webform PR engine (stdlib HTTP): Gogs has no PR API, so PR ops submit the
# same Web-UI forms a browser would — session login + CSRF + form POSTs.
# --------------------------------------------------------------------------- #
def webform_setup_message() -> str:
    return "\n".join([
        "Web form credentials missing: PR operations require GOGS_USERNAME and",
        "GOGS_PASSWORD (the same login you use in the Gogs Web UI).",
        "Add them next to GOGS_TOKEN in ~/.config/gogs-cli/config",
        "(or <repo-root>/.gogs.local.env):",
        "",
        "  GOGS_USERNAME=<your Gogs login name>",
        "  GOGS_PASSWORD=<your Gogs password>",
    ])


_CSRF_META_RE = re.compile(r'<meta\s+name="_csrf"\s+content="([^"]+)"')


def _strip_tags(html: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


class _WebformClient:
    """Minimal Gogs Web-UI client: login session, CSRF token, form POSTs.

    stdlib only (urllib + cookiejar) — runs anywhere Python runs: desktop,
    headless container, CI. Form fields and endpoints were sourced from the
    live Web UI.
    """

    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.jar = http.cookiejar.CookieJar()
        self.opener = request.build_opener(request.HTTPCookieProcessor(self.jar))
        self._logged_in = False

    # -- transport ---------------------------------------------------------- #
    def _open(self, url: str, data: bytes | None = None) -> tuple[str, str]:
        headers = {"User-Agent": USER_AGENT}
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        req = request.Request(url, data=data, method="POST" if data is not None else "GET",
                              headers=headers)
        try:
            with self.opener.open(req, timeout=30) as resp:
                return resp.geturl(), resp.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise SystemExit(f"webform {req.method} {url} failed: HTTP {exc.code}: {detail}") from exc
        except error.URLError as exc:
            raise SystemExit(f"webform {req.method} {url} failed: {exc.reason}") from exc

    def get(self, path: str) -> tuple[str, str]:
        return self._open(self.base + path)

    def post(self, path: str, fields: dict[str, str]) -> tuple[str, str]:
        # Redirects are followed (urllib re-issues GET on 302/301), so the
        # returned url is the FINAL destination — used to read back /pulls/N.
        return self._open(self.base + path, urlencode(fields).encode("utf-8"))

    # -- auth / csrf -------------------------------------------------------- #
    def csrf_of(self, html: str) -> str:
        m = _CSRF_META_RE.search(html)
        if not m:
            raise SystemExit("webform: no _csrf token found on page (unexpected page layout?)")
        return m.group(1)

    def ensure_login(self) -> None:
        if self._logged_in:
            return
        username = (os.environ.get("GOGS_USERNAME") or "").strip()
        password = os.environ.get("GOGS_PASSWORD") or ""
        if not username or not password:
            raise SystemExit(webform_setup_message())
        _, page = self.get("/user/login")
        final, body = self.post("/user/login", {
            "_csrf": self.csrf_of(page),
            "user_name": username,
            "password": password,
        })
        if "Signed in as" not in body and "/user/logout" not in body:
            raise SystemExit(
                "webform login to Gogs failed: verify GOGS_USERNAME/GOGS_PASSWORD "
                f"(post-login landed on {final})."
            )
        self._logged_in = True


@functools.lru_cache(maxsize=2)
def _webform(base: str) -> _WebformClient:
    return _WebformClient(base)


def _webform_for(t: Target) -> _WebformClient:
    wf = _webform(resolved_web_base())
    wf.ensure_login()
    return wf


# -- webform: pr list -------------------------------------------------------- #
_PULLS_ITEM_RE = re.compile(
    r'<li class="item">\s*<div class="ui black label">#(\d+)</div>\s*'
    r'<a class="title[^"]*" href="[^"]*">(.*?)</a>(.*?)</li>', re.S)
_PULLS_PAGE_RE = re.compile(r'[?&]page=(\d+)')


def webform_pr_list(t: Target, states: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Scrape /{owner}/{repo}/pulls tabs (Gogs issues list excludes PRs)."""
    wf = _webform_for(t)
    out: dict[str, list[dict[str, Any]]] = {}
    for state in states:
        rows: list[dict[str, Any]] = []
        page = 1
        while page <= 30:  # safety cap; 10 items/page, repos here have <= 3 pages
            _, html = wf.get(f"/{t.owner}/{t.repo}/pulls?type=all&state={state}&page={page}")
            items = _PULLS_ITEM_RE.findall(html)
            if not items:
                break
            for num, title, _rest in items:
                rows.append({"number": int(num), "title": _strip_tags(title).strip()})
            pages = [int(p) for p in _PULLS_PAGE_RE.findall(html)]
            if page >= max(pages or [page]):
                break
            page += 1
        out[state] = rows
    return out


# -- webform: pr create ------------------------------------------------------ #
def webform_pr_create(t: Target, base: str, head: str, title: str, body: str) -> int:
    """POST the compare-page form (the 'new pull request' dialog) directly."""
    wf = _webform_for(t)
    path = f"/{t.owner}/{t.repo}/compare/{quote(base, safe='/')}...{quote(head, safe='/')}"
    final, page = wf.get(path)
    existing = re.search(r"/pulls/(\d+)", final)
    if existing and f"/{t.owner}/{t.repo}/compare/" not in final:
        raise SystemExit(
            f"pr create failed: Gogs redirected the compare page to existing PR #{existing.group(1)} "
            f"({final}) — an open PR for these branches already exists."
        )
    if 'name="title"' not in page:
        # Gogs omits the form (no redirect) when an open PR already exists for
        # these branches — surface that link instead of a generic error.
        dup = re.search(rf'<a[^>]*href="[^"]*/pulls/(\d+)"[^>]*>\s*{re.escape(t.owner)}/{re.escape(t.repo)}#\d+', page)
        if dup:
            raise SystemExit(
                f"pr create failed: an open PR already exists for {base}...{head}: "
                f"#{dup.group(1)} ({final})."
            )
        raise SystemExit(
            "pr create failed: the compare page shows no new-PR form "
            f"(no diff between {base} and {head}, or the head branch is unknown)."
        )
    final2, body_html = wf.post(path, {
        "_csrf": wf.csrf_of(page),
        "title": title,
        "content": body or "",
    })
    m = re.search(r"/pulls/(\d+)", final2)
    if not m:
        flash = re.search(r'class="ui negative (?:flash )?message">(.*?)</div>', body_html, re.S)
        detail = _strip_tags(flash.group(1)).strip() if flash else final2
        raise SystemExit(f"pr create failed: no PR number in the redirect target. {detail[:300]}")
    return int(m.group(1))


# -- webform: pr merge ------------------------------------------------------- #
_MERGE_FORM_RE = re.compile(
    r'<form class="ui form" action="([^"]*/pulls/\d+/merge)" method="post">(.*?)</form>', re.S)
_MERGE_STYLE_RE = re.compile(r'name="merge_style"[^>]*value="([^"]+)"')
_WEBFORM_BANNER_RE = re.compile(
    r"nothing to merge|there is nothing|can.?t be merged|cannot be merged|conflict"
    r"|up-to-date|up to date|already merged|no changes|nothing to compare|unmerg", re.I)
_MERGE_BOX_RE = re.compile(r'<div class="comment merge box">(.*)<div class="comment form">', re.S)


def webform_pr_merge(t: Target, number: int, method: str) -> dict[str, Any]:
    """POST the merge form; returns {'clicked': bool, 'banner': str} for
    pr_merge()'s API verification path."""
    wf = _webform_for(t)
    _, page = wf.get(f"/{t.owner}/{t.repo}/pulls/{number}")
    form = _MERGE_FORM_RE.search(page)
    if not form:
        box = _MERGE_BOX_RE.search(page)
        region = box.group(1) if box else page
        text = _strip_tags(region)
        m = _WEBFORM_BANNER_RE.search(text)
        return {"clicked": False, "banner": text[max(0, m.start() - 60):m.end() + 120].strip() if m else text[:300].strip()}
    action, fields_html = form.group(1), form.group(2)
    styles = _MERGE_STYLE_RE.findall(fields_html)
    wanted = {"merge": "create_merge_commit", "squash": "squash", "rebase": "rebase"}[method]
    if wanted not in styles and styles:
        print(f"note: merge style '{method}' is not offered by this Gogs "
              f"(available: {', '.join(styles)}); falling back to '{styles[0]}'.", file=sys.stderr)
        wanted = styles[0]
    _, _after = wf.post(action, {
        "_csrf": wf.csrf_of(page),
        "merge_style": wanted,
        "commit_description": "",
    })
    return {"clicked": True, "banner": ""}


# -- webform: pr close / reopen ---------------------------------------------- #
_COMMENT_FORM_RE = re.compile(r'<form[^>]*action="([^"]*/issues/\d+/comments)"[^>]*method="post"')


def webform_pr_status(t: Target, number: int, verb: str) -> None:
    """Close/reopen = POST the comment form with the hidden `status` field set,
    exactly what the Web UI's Close/Reopen button does (content may be empty).
    status values are data-status-val from the UI: 'close' / 'reopen'."""
    status = {"close": "close", "reopen": "reopen"}[verb]
    wf = _webform_for(t)
    _, page = wf.get(f"/{t.owner}/{t.repo}/pulls/{number}")
    form = _COMMENT_FORM_RE.search(page)
    if not form:
        raise SystemExit(f"pr {verb} failed: no comment/status form on the PR page "
                         f"(PR #{number} may not exist or you lack access).")
    wf.post(form.group(1), {"content": "", "_csrf": wf.csrf_of(page), "status": status})


def pr_list(t: Target, state: str, base: str | None, head: str | None, limit: int, json_mode: bool) -> None:
    """Gogs has no PR list API (the issues list excludes PRs). Scrape /pulls."""
    if base or head:
        print("note: --base/--head are not supported by pr list (no PR list API); ignored.", file=sys.stderr)
    states = ["open", "closed"] if state == "all" else [state]
    data = webform_pr_list(t, states)
    rows = [{"number": r.get("number"), "title": r.get("title"), "state": s}
            for s in states for r in data.get(s, [])]
    emit(rows[:limit], json_mode, _fmt_pr_list)


def pr_create(t: Target, base: str, head: str | None, title: str, body: str, draft: bool, json_mode: bool) -> None:
    if not head:
        if not t.repo_root:
            raise SystemExit("pr create: --head is required (no git repo to read the current branch).")
        head = run_git(Path(t.repo_root), ["rev-parse", "--abbrev-ref", "HEAD"])
    if draft:
        print("note: Gogs Web UI has no draft concept; --draft ignored.", file=sys.stderr)
    number = webform_pr_create(t, base, head, title, body)
    verified = _verify_pr_state(t, int(number))
    if not verified.get("pull_request"):
        raise SystemExit(f"pr create: form reported #{number} but the issues API has no pull_request for it.")
    emit(verified, json_mode, _fmt_pr)


EXIT_SOFT = 2  # non-zero warning: operation diagnosed but did not succeed (e.g. empty-diff merge)

# Gogs merge-box banner classifiers (best-effort; raw banner is always surfaced too).
_NO_MERGE_EMPTY = re.compile(r"nothing to merge|there is nothing|up-to-date|up to date|already merged|no changes|nothing to compare", re.I)
_NO_MERGE_CONFLICT = re.compile(r"conflict|can.?t be merged|cannot be merged|unmerg", re.I)


def _classify_no_merge(banner: str) -> str:
    """Best-effort reason a merge could not proceed, derived from the Gogs banner text."""
    if not banner:
        return "not_mergeable"
    if _NO_MERGE_CONFLICT.search(banner):
        return "conflict"
    if _NO_MERGE_EMPTY.search(banner):
        return "empty_diff"
    return "not_mergeable"


def _emit_pr_warning(number: int, reason: str, banner: str, verified: dict[str, Any], json_mode: bool) -> None:
    """Report a soft-fail (non-zero, code 2): the PR was not merged, but the cause is known."""
    merged = bool((verified.get("pull_request") or {}).get("merged"))
    if json_mode:
        print(json.dumps({"warning": reason, "reason": reason, "banner": banner,
                          "merged": merged, "number": number, "state": verified.get("state")},
                         ensure_ascii=False, indent=2))
    elif reason == "already_merged":
        print(f"warning: PR #{number} is already merged; nothing to do.", file=sys.stderr)
    elif banner:
        print(f"warning: PR #{number} was not merged ({reason}): {banner}", file=sys.stderr)
    else:
        print(f"warning: PR #{number} was not merged ({reason})", file=sys.stderr)
    sys.exit(EXIT_SOFT)


def pr_merge(t: Target, number: int, method: str, delete_branch: bool, json_mode: bool) -> None:
    out = webform_pr_merge(t, number, method)
    data = {"ok": out.get("clicked"), "clicked": out.get("clicked"), "banner": out.get("banner", "")}
    verified = _verify_pr_state(t, number)
    pr = verified.get("pull_request") or {}
    if not pr:
        raise SystemExit(f"pr merge failed: #{number} is not a pull request (issues API has no pull_request).")
    # No merge form — Gogs won't merge this. Classify and warn (non-zero), don't hard-fail.
    if not (data.get("ok") and data.get("clicked")):
        banner = (data.get("banner") or "").strip()
        reason = "already_merged" if pr.get("merged") else _classify_no_merge(banner)
        _emit_pr_warning(number, reason, banner, verified, json_mode)
    # Merge form was submitted — verify via the issues API that it actually merged.
    if not pr.get("merged"):
        raise SystemExit(f"pr merge: form submitted but the issues API still reports unmerged for #{number}.")
    if delete_branch:
        print("note: --delete-branch is not automated for Gogs; delete the branch manually if needed.", file=sys.stderr)
    emit(verified, json_mode, _fmt_pr)


def _pr_button_action(t: Target, number: int, verb: str, expected_state: str, json_mode: bool) -> None:
    webform_pr_status(t, number, verb)  # raises SystemExit on failure
    verified = _verify_pr_state(t, number)
    if verified.get("state") != expected_state:
        raise SystemExit(f"pr {verb}: form submitted but the issues API reports state '{verified.get('state')}' (expected '{expected_state}') for #{number}.")
    emit(verified, json_mode, _fmt_pr)


def pr_close(t: Target, number: int, json_mode: bool) -> None:
    _pr_button_action(t, number, "close", "closed", json_mode)


def pr_reopen(t: Target, number: int, json_mode: bool) -> None:
    _pr_button_action(t, number, "reopen", "open", json_mode)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _add_target(p: argparse.ArgumentParser) -> None:
    p.add_argument("repo", nargs="?", help="OWNER/REPO. Default: origin remote.")
    p.add_argument("--remote", default="origin")
    p.add_argument("--repo-root", help="Path inside a repo when cwd is not the repo root.")
    p.add_argument("--json", action="store_true", help="Machine-readable JSON output.")


def _add_body(p: argparse.ArgumentParser) -> None:
    p.add_argument("--body", default="")
    p.add_argument("-F", "--body-file", dest="body_file")


def _tgt(args: argparse.Namespace) -> Target:
    return resolve_target(getattr(args, "repo", None), getattr(args, "remote", "origin"), getattr(args, "repo_root", None))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gogs-cli",
        description="Reusable Gogs capability CLI (gh-style). Stateless — the remote Gogs instance is the single source of truth.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
common workflows (params via `gogs-cli <cmd> --help`; OWNER/REPO defaults to origin):

  issue -> branch -> PR -> merge -> close:
    gogs-cli issue create --title "Fix reconnect" --body-file body.md
    gogs-cli issue develop 29                  # -> branch 29-fix-reconnect (off develop)
    gogs-cli pr create --title "Fix reconnect" # base=develop, head=current branch
    gogs-cli pr view <n>                       # verify Open
    gogs-cli pr merge <n>                      # after acceptance; verify Merged
    gogs-cli issue comment 29 --body "result..."; gogs-cli issue close 29

  search before creating (avoid duplicates):
    gogs-cli issue list --state open

host & auth: the web base comes from GOGS_BASE_URL (probed live); extra addresses
via GOGS_FALLBACK_URLS let the same invocation work from several networks.
PR ops submit the Web UI forms directly (Gogs has no PR API;
needs GOGS_USERNAME/GOGS_PASSWORD) and verify each write via the issues API
before reporting success.
Add --json anywhere for machine-readable output.
""",
    )
    sub = parser.add_subparsers(dest="resource", required=True)

    # repo -------------------------------------------------------------------
    repo = sub.add_parser("repo", help="Repository (API, read-only).")
    repo_sub = repo.add_subparsers(dest="action", required=True)

    rv = repo_sub.add_parser("view", help="View a repository.")
    _add_target(rv)
    rv.set_defaults(func=lambda a: repo_view(_tgt(a), a.json))

    rl = repo_sub.add_parser("list", help="List repositories.")
    rl.add_argument("--user", help="List this user's repos. Default: authenticated user.")
    rl.add_argument("--limit", type=int, default=30)
    rl.add_argument("--remote", default="origin")
    rl.add_argument("--repo-root")
    rl.add_argument("--json", action="store_true")
    rl.set_defaults(func=lambda a: repo_list(a.user, a.limit, a.json, a.remote, a.repo_root))

    # issue ------------------------------------------------------------------
    issue = sub.add_parser("issue", help="Issues (API).")
    issue_sub = issue.add_subparsers(dest="action", required=True)

    il = issue_sub.add_parser("list", help="List issues.")
    _add_target(il)
    il.add_argument("--state", choices=["open", "closed", "all"], default="open")
    il.add_argument("--label", action="append", default=[], help="Label name (repeatable).")
    il.add_argument("--assignee")
    il.add_argument("--limit", type=int, default=30)
    il.set_defaults(func=lambda a: issue_list(_tgt(a), a.state, a.label, a.assignee, a.limit, a.json))

    iv = issue_sub.add_parser("view", help="View an issue.")
    _add_target(iv)
    iv.add_argument("number", type=int)
    iv.set_defaults(func=lambda a: issue_view(_tgt(a), a.number, a.json))

    ic = issue_sub.add_parser("create", help="Create an issue.")
    _add_target(ic)
    ic.add_argument("--title", required=True)
    _add_body(ic)
    ic.add_argument("--assignee")
    ic.add_argument("--milestone", type=int)
    ic.add_argument("--label", action="append", default=[], help="Label name (repeatable).")
    ic.add_argument("--closed", action="store_true")
    ic.set_defaults(func=lambda a: issue_create(_tgt(a), a.title, read_text(a.body, a.body_file), a.assignee, a.milestone, a.label, a.closed, a.json))

    ie = issue_sub.add_parser("edit", help="Edit an issue (title/body/state).")
    _add_target(ie)
    ie.add_argument("number", type=int)
    ie.add_argument("--title")
    ie.add_argument("--body", default=None)
    ie.add_argument("-F", "--body-file", dest="body_file")
    ie.add_argument("--state", choices=["open", "closed"])
    ie.set_defaults(func=lambda a: issue_edit(_tgt(a), a.number, a.title, read_text(a.body, a.body_file) if a.body_file else a.body, a.state, a.json))

    icl = issue_sub.add_parser("close", help="Close an issue.")
    _add_target(icl)
    icl.add_argument("number", type=int)
    icl.set_defaults(func=lambda a: issue_set_state(_tgt(a), a.number, "closed", a.json))

    iro = issue_sub.add_parser("reopen", help="Reopen an issue.")
    _add_target(iro)
    iro.add_argument("number", type=int)
    iro.set_defaults(func=lambda a: issue_set_state(_tgt(a), a.number, "open", a.json))

    icm = issue_sub.add_parser("comment", help="Comment on an issue.")
    _add_target(icm)
    icm.add_argument("number", type=int)
    _add_body(icm)
    icm.set_defaults(func=lambda a: issue_comment(_tgt(a), a.number, read_text(a.body, a.body_file), a.json))

    idv = issue_sub.add_parser("develop", help="Create a local branch off --base named from an issue (gh-style).")
    _add_target(idv)
    idv.add_argument("number", type=int)
    idv.add_argument("--base", default="develop", help="Branch to start from. Default: develop.")
    idv.add_argument("--name", help="Override branch name (default: <number>-<title-slug>).")
    idv.set_defaults(func=lambda a: issue_develop(_tgt(a), a.number, a.base, a.name, a.json))

    # label ------------------------------------------------------------------
    label = sub.add_parser("label", help="Labels (API).")
    label_sub = label.add_subparsers(dest="action", required=True)
    ll = label_sub.add_parser("list", help="List labels.")
    _add_target(ll)
    ll.set_defaults(func=lambda a: label_list(_tgt(a), a.json))

    # pr ---------------------------------------------------------------------
    pr = sub.add_parser("pr", help="Pull requests (view via API; list/create/merge/close/reopen via Web UI forms).")
    pr_sub = pr.add_subparsers(dest="action", required=True)

    pl = pr_sub.add_parser("list", help="List pull requests (Web UI scrape; no PR list API).")
    _add_target(pl)
    pl.add_argument("--state", choices=["open", "closed", "all"], default="open")
    pl.add_argument("--base")
    pl.add_argument("--head")
    pl.add_argument("--limit", type=int, default=30)
    pl.set_defaults(func=lambda a: pr_list(_tgt(a), a.state, a.base, a.head, a.limit, a.json))

    pv = pr_sub.add_parser("view", help="View a pull request.")
    _add_target(pv)
    pv.add_argument("number", type=int)
    pv.set_defaults(func=lambda a: pr_view(_tgt(a), a.number, a.json))

    pc = pr_sub.add_parser("create", help="Create a pull request (Web UI form).")
    _add_target(pc)
    pc.add_argument("--base", default="develop", help="Base (target) branch. Default: develop.")
    pc.add_argument("--head", help="Head (source) branch. Default: current git branch.")
    pc.add_argument("--title", required=True)
    _add_body(pc)
    pc.add_argument("--draft", action="store_true")
    pc.set_defaults(func=lambda a: pr_create(_tgt(a), a.base, a.head, a.title, read_text(a.body, a.body_file), a.draft, a.json))

    pm = pr_sub.add_parser("merge", help="Merge a pull request (Web UI form).")
    _add_target(pm)
    pm.add_argument("number", type=int)
    method = pm.add_mutually_exclusive_group()
    method.add_argument("--merge", action="store_const", const="merge", dest="method")
    method.add_argument("--squash", action="store_const", const="squash", dest="method")
    method.add_argument("--rebase", action="store_const", const="rebase", dest="method")
    pm.set_defaults(method="merge")
    pm.add_argument("--delete-branch", action="store_true")
    pm.set_defaults(func=lambda a: pr_merge(_tgt(a), a.number, a.method, a.delete_branch, a.json))

    pcl = pr_sub.add_parser("close", help="Close a pull request (Web UI form).")
    _add_target(pcl)
    pcl.add_argument("number", type=int)
    pcl.set_defaults(func=lambda a: pr_close(_tgt(a), a.number, a.json))

    pro = pr_sub.add_parser("reopen", help="Reopen a pull request (Web UI form).")
    _add_target(pro)
    pro.add_argument("number", type=int)
    pro.set_defaults(func=lambda a: pr_reopen(_tgt(a), a.number, a.json))

    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
