"""tasks_github — async gh CLI + GitHub GraphQL client for Projects v2.

Reuses :mod:`library_github` for auth, env, and subprocess plumbing.
Two in-process caches with asyncio locks keep the hot read path cheap:

* schema cache (5 min TTL) — keyed by project node id
* items cache (30 s TTL) — keyed by project node id

All write helpers bust the items cache.  Errors raise :class:`TasksGithubError`
with a ``status_hint`` so route handlers can map cleanly to HTTP codes.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from . import library_github as gh_lib
from .tasks_config import TasksConfig

# ── constants ───────────────────────────────────────────────────

SCHEMA_TTL_S = 300.0
ITEMS_TTL_S = 30.0
REPOS_TTL_S = 600.0
GRAPHQL_TIMEOUT_S = 20.0

_AREA_LABEL_RE = re.compile(r"^area:([a-z0-9][a-z0-9-]*)$")
_PROJ_LABEL_RE = re.compile(r"^proj:([a-z0-9][a-z0-9-]*)$")

_FIELD_STATUS = "Status"
_FIELD_PRIORITY = "Priority"
_FIELD_CATEGORY = "Category"
_FIELD_DUE = "Due Date"
_FIELD_WAITING_ON = "Waiting On"
_FIELD_SOURCE = "Source"


# ── errors ──────────────────────────────────────────────────────


class TasksGithubError(Exception):
    """gh / GraphQL failure with classification."""

    def __init__(
        self,
        kind: Literal["auth", "schema", "validation", "network", "graphql", "parse", "other"],
        message: str,
        *,
        status_hint: int = 424,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status_hint = status_hint
        self.detail = detail


# ── schema + caches ─────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectSchema:
    project_node_id: str
    field_id_by_name: dict[str, str]
    field_type_by_name: dict[str, str]
    option_id_by_field_option: dict[tuple[str, str], str]  # (field_name, option_name) → option_id
    options_by_field: dict[str, list[str]]
    fetched_at: float

    def field_id(self, name: str) -> str:
        try:
            return self.field_id_by_name[name]
        except KeyError as exc:
            raise TasksGithubError(
                "schema",
                f"Field {name!r} not found in project",
                status_hint=422,
                detail={"available_fields": sorted(self.field_id_by_name)},
            ) from exc

    def option_id(self, field_name: str, option_name: str) -> str:
        try:
            return self.option_id_by_field_option[(field_name, option_name)]
        except KeyError as exc:
            raise TasksGithubError(
                "schema",
                f"Option {option_name!r} not found in field {field_name!r}",
                status_hint=422,
                detail={"available": self.options_by_field.get(field_name, [])},
            ) from exc


@dataclass
class _Cache:
    data: Any = None
    fetched_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_schema_caches: dict[str, _Cache] = {}
_items_caches: dict[str, _Cache] = {}
_repos_caches: dict[str, _Cache] = {}
_node_id_cache: dict[tuple[str, str, int], str] = {}


def _schema_slot(project_node_id: str) -> _Cache:
    if project_node_id not in _schema_caches:
        _schema_caches[project_node_id] = _Cache()
    return _schema_caches[project_node_id]


def _items_slot(project_node_id: str) -> _Cache:
    if project_node_id not in _items_caches:
        _items_caches[project_node_id] = _Cache()
    return _items_caches[project_node_id]


def _repos_slot(login: str) -> _Cache:
    if login not in _repos_caches:
        _repos_caches[login] = _Cache()
    return _repos_caches[login]


def invalidate_caches(
    *,
    project_node_id: str | None = None,
    schema: bool = False,
    items: bool = True,
    repos: bool = False,
) -> None:
    """Drop cached entries. Default: bust items only (write path)."""
    if project_node_id:
        if schema and project_node_id in _schema_caches:
            _schema_caches[project_node_id].data = None
            _schema_caches[project_node_id].fetched_at = 0.0
        if items and project_node_id in _items_caches:
            _items_caches[project_node_id].data = None
            _items_caches[project_node_id].fetched_at = 0.0
    else:
        if schema:
            for slot in _schema_caches.values():
                slot.data = None
                slot.fetched_at = 0.0
        if items:
            for slot in _items_caches.values():
                slot.data = None
                slot.fetched_at = 0.0
    if repos:
        for slot in _repos_caches.values():
            slot.data = None
            slot.fetched_at = 0.0


# ── auth ────────────────────────────────────────────────────────


def auth_check() -> dict:
    """Reuse library_github.gh_auth_check; require ``project`` scope."""
    out = gh_lib.gh_auth_check()
    scopes = out.get("scopes") or []
    has_project = any(s == "project" or s == "read:project" for s in scopes)
    return {**out, "has_project_scope": has_project}


def _require_auth() -> None:
    state = auth_check()
    if not state.get("ok"):
        raise TasksGithubError(
            "auth",
            f"gh CLI not authenticated: {state.get('error') or 'unknown'}",
        )
    if not state.get("has_project_scope"):
        raise TasksGithubError(
            "auth",
            "gh token missing the 'project' scope — run `gh auth refresh -s project`",
        )


def _reset_auth_state() -> None:
    """Force the next auth_check to re-run (used on 401/Bad-credentials)."""
    gh_lib._auth_state.update({"checked": False, "ok": False})


# ── GraphQL / CLI wrappers ──────────────────────────────────────


async def _graphql(
    query: str,
    variables: dict[str, Any] | None = None,
    *,
    timeout: float = GRAPHQL_TIMEOUT_S,
) -> dict[str, Any]:
    args = ["api", "graphql", "-f", f"query={query}"]
    for k, v in (variables or {}).items():
        if isinstance(v, bool):
            args.extend(["-F", f"{k}={'true' if v else 'false'}"])
        elif isinstance(v, (int, float)):
            args.extend(["-F", f"{k}={v}"])
        else:
            args.extend(["-f", f"{k}={v}"])
    rc, stdout, stderr = await gh_lib._gh_run(args, timeout_s=timeout)
    if rc != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        if _is_auth_error(err):
            _reset_auth_state()
            raise TasksGithubError("auth", f"github auth failed: {err[:300]}")
        raise TasksGithubError("network", f"gh rc={rc}: {err[:500]}")
    try:
        payload = json.loads(stdout.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise TasksGithubError("parse", f"GraphQL response was not JSON: {exc}") from exc
    if "errors" in payload:
        msg = json.dumps(payload["errors"])[:500]
        raise TasksGithubError("graphql", f"GraphQL errors: {msg}", detail=payload["errors"])
    return payload.get("data") or {}


async def _gh_cli(args: list[str], *, timeout: float = GRAPHQL_TIMEOUT_S) -> str:
    rc, stdout, stderr = await gh_lib._gh_run(args, timeout_s=timeout)
    if rc != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        if _is_auth_error(err):
            _reset_auth_state()
            raise TasksGithubError("auth", f"github auth failed: {err[:300]}")
        raise TasksGithubError("network", f"gh rc={rc}: {err[:500]}")
    return stdout.decode("utf-8", errors="replace")


def _is_auth_error(err: str) -> bool:
    low = err.lower()
    return "bad credentials" in low or "401" in low or "authentication" in low


# ── project node id resolution ──────────────────────────────────


async def resolve_project_node_id(cfg: TasksConfig) -> str:
    """Return the project's GraphQL node id. Uses ``cfg.project_node_id`` if set."""
    if cfg.project_node_id:
        return cfg.project_node_id
    if not cfg.project_owner or cfg.project_number <= 0:
        raise TasksGithubError(
            "validation",
            "tasks project is not configured (set tasks.project_url in override.yaml)",
            status_hint=400,
        )
    key = (cfg.project_owner_type, cfg.project_owner, cfg.project_number)
    if key in _node_id_cache:
        return _node_id_cache[key]
    query = """
        query($login: String!, $number: Int!) {
            user(login: $login)         { projectV2(number: $number) { id } }
            organization(login: $login) { projectV2(number: $number) { id } }
        }
    """
    data = await _graphql(
        query,
        {"login": cfg.project_owner, "number": cfg.project_number},
    )
    node_id = (
        (data.get("user") or {}).get("projectV2", {}).get("id")
        or (data.get("organization") or {}).get("projectV2", {}).get("id")
    )
    if not node_id:
        raise TasksGithubError(
            "schema",
            f"Project #{cfg.project_number} not found under {cfg.project_owner}",
            status_hint=404,
        )
    _node_id_cache[key] = node_id
    return node_id


# ── schema ──────────────────────────────────────────────────────


_SCHEMA_QUERY = """
    query($projectId: ID!) {
        node(id: $projectId) {
            ... on ProjectV2 {
                fields(first: 50) {
                    nodes {
                        ... on ProjectV2Field            { id name dataType }
                        ... on ProjectV2SingleSelectField{ id name dataType options { id name } }
                        ... on ProjectV2IterationField   { id name dataType }
                    }
                }
            }
        }
    }
"""


def _parse_schema(project_node_id: str, payload: dict[str, Any]) -> ProjectSchema:
    nodes = (((payload.get("node") or {}).get("fields") or {}).get("nodes")) or []
    field_id_by_name: dict[str, str] = {}
    field_type_by_name: dict[str, str] = {}
    option_id: dict[tuple[str, str], str] = {}
    options_by_field: dict[str, list[str]] = {}
    for node in nodes:
        name = node.get("name")
        if not isinstance(name, str) or not name:
            continue
        field_id_by_name[name] = node["id"]
        field_type_by_name[name] = node.get("dataType") or ""
        opts = node.get("options")
        if isinstance(opts, list):
            names: list[str] = []
            for opt in opts:
                opt_name = opt.get("name")
                opt_id = opt.get("id")
                if isinstance(opt_name, str) and isinstance(opt_id, str):
                    option_id[(name, opt_name)] = opt_id
                    names.append(opt_name)
            options_by_field[name] = names
    return ProjectSchema(
        project_node_id=project_node_id,
        field_id_by_name=field_id_by_name,
        field_type_by_name=field_type_by_name,
        option_id_by_field_option=option_id,
        options_by_field=options_by_field,
        fetched_at=time.time(),
    )


async def fetch_project_schema(cfg: TasksConfig, *, force: bool = False) -> ProjectSchema:
    project_node_id = await resolve_project_node_id(cfg)
    slot = _schema_slot(project_node_id)
    async with slot.lock:
        fresh = (
            slot.data is not None
            and not force
            and (time.time() - slot.fetched_at) < SCHEMA_TTL_S
        )
        if fresh:
            return slot.data
        data = await _graphql(_SCHEMA_QUERY, {"projectId": project_node_id})
        schema = _parse_schema(project_node_id, data)
        slot.data = schema
        slot.fetched_at = schema.fetched_at
        return schema


# ── items ───────────────────────────────────────────────────────


_BULK_ITEMS_QUERY = """
    query($projectId: ID!, $cursor: String) {
        node(id: $projectId) {
            ... on ProjectV2 {
                items(first: 100, after: $cursor) {
                    pageInfo { hasNextPage endCursor }
                    nodes {
                        id
                        content {
                            __typename
                            ... on Issue {
                                id number title state url
                                createdAt updatedAt closedAt
                                repository { nameWithOwner }
                                labels(first: 20) { nodes { name color } }
                                assignees(first: 5) { nodes { login avatarUrl } }
                            }
                        }
                        fieldValues(first: 30) {
                            nodes {
                                ... on ProjectV2ItemFieldTextValue {
                                    text
                                    field { ... on ProjectV2FieldCommon { name } }
                                }
                                ... on ProjectV2ItemFieldDateValue {
                                    date
                                    field { ... on ProjectV2FieldCommon { name } }
                                }
                                ... on ProjectV2ItemFieldSingleSelectValue {
                                    name
                                    field { ... on ProjectV2FieldCommon { name } }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
"""


def _extract_field_values(nodes: list[dict[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for fv in nodes or []:
        fname = (fv.get("field") or {}).get("name")
        if not isinstance(fname, str) or not fname:
            continue
        value = fv.get("text") or fv.get("date") or fv.get("name")
        if isinstance(value, str) and value:
            out[fname] = value
    return out


def _classify_labels(labels: list[str]) -> tuple[str | None, str | None, list[str]]:
    """Return (area_slug, proj_slug, other_labels)."""
    area: str | None = None
    proj: str | None = None
    rest: list[str] = []
    for lab in labels:
        ma = _AREA_LABEL_RE.match(lab)
        if ma:
            area = area or ma.group(1)
            continue
        mp = _PROJ_LABEL_RE.match(lab)
        if mp:
            proj = proj or mp.group(1)
            continue
        rest.append(lab)
    return area, proj, rest


def _build_task_item(node: dict[str, Any]) -> dict[str, Any] | None:
    content = node.get("content") or {}
    if content.get("__typename") != "Issue":
        return None  # skip drafts / PRs
    number = content.get("number")
    if not isinstance(number, int):
        return None
    repo = (content.get("repository") or {}).get("nameWithOwner") or ""
    labels_raw = (content.get("labels") or {}).get("nodes") or []
    labels = [l.get("name") for l in labels_raw if isinstance(l.get("name"), str)]
    area_slug, proj_slug, _other = _classify_labels(labels)
    fields = _extract_field_values((node.get("fieldValues") or {}).get("nodes") or [])
    assignees = (content.get("assignees") or {}).get("nodes") or []
    return {
        "project_item_id": node.get("id"),
        "issue_node_id": content.get("id"),
        "repo": repo,
        "number": number,
        "title": content.get("title") or "",
        "state": (content.get("state") or "OPEN").upper(),
        "url": content.get("url") or "",
        "labels": labels,
        "labels_meta": [{"name": l.get("name"), "color": l.get("color")} for l in labels_raw],
        "assignees": [
            {"login": a.get("login"), "avatar_url": a.get("avatarUrl")}
            for a in assignees if isinstance(a, dict)
        ],
        "status": fields.get(_FIELD_STATUS),
        "priority": fields.get(_FIELD_PRIORITY),
        "category": fields.get(_FIELD_CATEGORY),
        "due_date": fields.get(_FIELD_DUE),
        "waiting_on": fields.get(_FIELD_WAITING_ON),
        "source": fields.get(_FIELD_SOURCE),
        "created_at": content.get("createdAt"),
        "updated_at": content.get("updatedAt"),
        "closed_at": content.get("closedAt"),
        "area_slug": area_slug,
        "proj_slug": proj_slug,
    }


async def _fetch_all_items(project_node_id: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        variables: dict[str, Any] = {"projectId": project_node_id}
        if cursor:
            variables["cursor"] = cursor
        data = await _graphql(_BULK_ITEMS_QUERY, variables)
        page = (data.get("node") or {}).get("items") or {}
        for raw in page.get("nodes") or []:
            built = _build_task_item(raw)
            if built is not None:
                items.append(built)
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")
        if not cursor:
            break
    return items


async def list_items(
    cfg: TasksConfig,
    *,
    force: bool = False,
    force_max_age: float | None = None,
) -> dict[str, Any]:
    """Return ``{items, cached_at, fresh}``. Always re-fetches if ``force``."""
    project_node_id = await resolve_project_node_id(cfg)
    slot = _items_slot(project_node_id)
    async with slot.lock:
        age = time.time() - slot.fetched_at
        ttl = ITEMS_TTL_S if force_max_age is None else min(ITEMS_TTL_S, force_max_age)
        if slot.data is not None and not force and age < ttl:
            return {"items": slot.data, "cached_at": slot.fetched_at, "fresh": False}
        items = await _fetch_all_items(project_node_id)
        slot.data = items
        slot.fetched_at = time.time()
        return {"items": items, "cached_at": slot.fetched_at, "fresh": True}


async def get_item(
    cfg: TasksConfig,
    *,
    issue_node_id: str | None = None,
    repo: str | None = None,
    number: int | None = None,
) -> dict[str, Any] | None:
    """Locate a single task by node id or (repo, number) — looks at the items cache first."""
    listing = await list_items(cfg)
    for item in listing["items"]:
        if issue_node_id and item.get("issue_node_id") == issue_node_id:
            return item
        if repo and number and item.get("repo") == repo and item.get("number") == number:
            return item
    return None


async def fetch_issue_body(repo: str, number: int) -> str:
    """Single-issue body fetch — separate so list calls stay light."""
    out = await _gh_cli(
        ["issue", "view", str(number), "--repo", repo, "--json", "body"],
    )
    try:
        data = json.loads(out or "{}")
    except json.JSONDecodeError:
        return ""
    body = data.get("body")
    return body if isinstance(body, str) else ""


# ── repos picker ────────────────────────────────────────────────


_USER_REPOS_QUERY = """
    query($cursor: String) {
        viewer {
            repositories(first: 100, after: $cursor, ownerAffiliations: [OWNER], orderBy: { field: UPDATED_AT, direction: DESC }) {
                pageInfo { hasNextPage endCursor }
                nodes { nameWithOwner isArchived isPrivate }
            }
        }
    }
"""


async def list_repos_for_user() -> list[dict[str, Any]]:
    """Owner-only repos for the authed user, cached 10 min."""
    state = auth_check()
    login = state.get("user") or "viewer"
    slot = _repos_slot(login)
    async with slot.lock:
        if slot.data is not None and (time.time() - slot.fetched_at) < REPOS_TTL_S:
            return slot.data
        repos: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            data = await _graphql(_USER_REPOS_QUERY, {"cursor": cursor} if cursor else None)
            page = (data.get("viewer") or {}).get("repositories") or {}
            for raw in page.get("nodes") or []:
                if raw.get("isArchived"):
                    continue
                name = raw.get("nameWithOwner")
                if isinstance(name, str):
                    repos.append({"name": name, "private": bool(raw.get("isPrivate"))})
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            cursor = info.get("endCursor")
            if not cursor:
                break
        slot.data = repos
        slot.fetched_at = time.time()
        return repos


# ── write helpers ───────────────────────────────────────────────


@dataclass
class CreatedIssue:
    issue_node_id: str
    number: int
    url: str
    repo: str


async def create_issue(
    cfg: TasksConfig,
    *,
    repo: str,
    title: str,
    body: str = "",
    labels: list[str] | None = None,
) -> CreatedIssue:
    if not repo or "/" not in repo:
        raise TasksGithubError("validation", f"invalid repo {repo!r}", status_hint=400)
    await ensure_labels(repo, labels)
    args = ["issue", "create", "--repo", repo, "--title", title, "--body", body or ""]
    for lab in labels or []:
        if isinstance(lab, str) and lab:
            args.extend(["--label", lab])
    url_out = (await _gh_cli(args)).strip()
    if not url_out:
        raise TasksGithubError("network", "gh issue create returned no URL")
    url = url_out.split()[-1]  # gh sometimes prints "Creating issue …\n<url>"
    try:
        number = int(url.rstrip("/").split("/")[-1])
    except ValueError as exc:
        raise TasksGithubError("parse", f"could not parse issue number from {url!r}") from exc
    view = json.loads(
        await _gh_cli(["issue", "view", str(number), "--repo", repo, "--json", "id,url"])
    )
    invalidate_caches(project_node_id=await resolve_project_node_id(cfg))
    return CreatedIssue(
        issue_node_id=view["id"],
        number=number,
        url=view.get("url") or url,
        repo=repo,
    )


_ADD_TO_PROJECT_MUT = """
    mutation($projectId: ID!, $contentId: ID!) {
        addProjectV2ItemById(input: { projectId: $projectId, contentId: $contentId }) {
            item { id }
        }
    }
"""


async def add_to_project(cfg: TasksConfig, issue_node_id: str) -> str:
    project_node_id = await resolve_project_node_id(cfg)
    data = await _graphql(
        _ADD_TO_PROJECT_MUT,
        {"projectId": project_node_id, "contentId": issue_node_id},
    )
    item = ((data.get("addProjectV2ItemById") or {}).get("item")) or {}
    item_id = item.get("id")
    if not item_id:
        raise TasksGithubError("graphql", "addProjectV2ItemById returned no item id")
    invalidate_caches(project_node_id=project_node_id)
    return item_id


_UPDATE_FIELD_MUT_SS = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $val: String!) {
        updateProjectV2ItemFieldValue(input: {
            projectId: $projectId, itemId: $itemId, fieldId: $fieldId,
            value: { singleSelectOptionId: $val }
        }) { projectV2Item { id } }
    }
"""

_UPDATE_FIELD_MUT_DATE = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $val: Date!) {
        updateProjectV2ItemFieldValue(input: {
            projectId: $projectId, itemId: $itemId, fieldId: $fieldId,
            value: { date: $val }
        }) { projectV2Item { id } }
    }
"""

_UPDATE_FIELD_MUT_TEXT = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!, $val: String!) {
        updateProjectV2ItemFieldValue(input: {
            projectId: $projectId, itemId: $itemId, fieldId: $fieldId,
            value: { text: $val }
        }) { projectV2Item { id } }
    }
"""

_CLEAR_FIELD_MUT = """
    mutation($projectId: ID!, $itemId: ID!, $fieldId: ID!) {
        clearProjectV2ItemFieldValue(input: {
            projectId: $projectId, itemId: $itemId, fieldId: $fieldId
        }) { projectV2Item { id } }
    }
"""


async def _set_field(
    cfg: TasksConfig,
    schema: ProjectSchema,
    item_id: str,
    field_name: str,
    value: str | None,
) -> None:
    project_node_id = schema.project_node_id
    field_id = schema.field_id(field_name)
    field_type = schema.field_type_by_name.get(field_name, "")
    if value in (None, ""):
        await _graphql(
            _CLEAR_FIELD_MUT,
            {"projectId": project_node_id, "itemId": item_id, "fieldId": field_id},
        )
        return
    if field_type == "SINGLE_SELECT":
        option_id = schema.option_id(field_name, value)
        await _graphql(
            _UPDATE_FIELD_MUT_SS,
            {"projectId": project_node_id, "itemId": item_id, "fieldId": field_id, "val": option_id},
        )
    elif field_type == "DATE":
        await _graphql(
            _UPDATE_FIELD_MUT_DATE,
            {"projectId": project_node_id, "itemId": item_id, "fieldId": field_id, "val": value},
        )
    elif field_type == "TEXT":
        await _graphql(
            _UPDATE_FIELD_MUT_TEXT,
            {"projectId": project_node_id, "itemId": item_id, "fieldId": field_id, "val": value},
        )
    else:
        raise TasksGithubError(
            "schema",
            f"unsupported field type {field_type!r} for {field_name!r}",
            status_hint=422,
        )


async def update_fields(
    cfg: TasksConfig,
    item_id: str,
    patch: dict[str, str | None],
) -> None:
    """Apply a {field_name: value} patch. ``None``/``""`` clears the field.

    Auto-retries once on schema mismatch (refetches schema, in case the user
    added/renamed options in the GH UI).
    """
    if not patch:
        return
    project_node_id = await resolve_project_node_id(cfg)
    schema = await fetch_project_schema(cfg)
    # Each field targets a distinct fieldId — the mutations are independent, so
    # apply them CONCURRENTLY instead of one GraphQL round-trip per field.
    # Idempotent (sets the same value), so the schema-mismatch retry can safely
    # re-run the whole patch. Use return_exceptions=True so that when several
    # fields fail at once we (a) collect ALL results — no orphaned task whose
    # exception is "never retrieved" — and (b) decide deterministically: a
    # non-schema error propagates; if every error is a schema mismatch, refetch
    # the schema once and retry the whole patch.
    results = await asyncio.gather(*(
        _set_field(cfg, schema, item_id, name, value)
        for name, value in patch.items()
    ), return_exceptions=True)
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        non_schema = [
            e for e in errors
            if not (isinstance(e, TasksGithubError) and e.kind == "schema")
        ]
        if non_schema:
            raise non_schema[0]
        # Every failure was a schema mismatch — refetch + retry the whole patch.
        schema = await fetch_project_schema(cfg, force=True)
        await asyncio.gather(*(
            _set_field(cfg, schema, item_id, name, value)
            for name, value in patch.items()
        ))
    invalidate_caches(project_node_id=project_node_id)


async def find_item_id_for_issue(cfg: TasksConfig, repo: str, number: int) -> str:
    """Locate the project item id for an issue. Cache first, then GraphQL fallback."""
    item = await get_item(cfg, repo=repo, number=number)
    if item and item.get("project_item_id"):
        return item["project_item_id"]
    project_node_id = await resolve_project_node_id(cfg)
    owner, name = repo.split("/", 1)
    data = await _graphql(
        """
        query($owner: String!, $name: String!, $number: Int!) {
            repository(owner: $owner, name: $name) {
                issue(number: $number) {
                    projectItems(first: 20) { nodes { id project { id } } }
                }
            }
        }
        """,
        {"owner": owner, "name": name, "number": number},
    )
    issue = ((data.get("repository") or {}).get("issue")) or {}
    for node in (issue.get("projectItems") or {}).get("nodes") or []:
        if ((node.get("project") or {}).get("id")) == project_node_id:
            return node["id"]
    raise TasksGithubError(
        "schema",
        f"issue {repo}#{number} is not on the configured project",
        status_hint=409,
    )


# ── auto-provision area:/proj: labels ───────────────────────────

# Per-process cache of the labels known to exist in a repo, so we run at
# most one `gh label list` per repo and skip re-creating labels we've
# already ensured this run.
_repo_labels_cache: dict[str, set[str]] = {}

# Stable palette so a freshly auto-created label isn't a random gh color.
_AREA_LABEL_COLOR = "5319e7"
_PROJ_LABEL_COLOR = "1d76db"


async def _repo_labels(repo: str) -> set[str]:
    cached = _repo_labels_cache.get(repo)
    if cached is not None:
        return cached
    try:
        out = await _gh_cli(["label", "list", "--repo", repo, "--limit", "300", "--json", "name"])
        names = {row["name"] for row in json.loads(out) if isinstance(row.get("name"), str)}
    except Exception:
        # If listing fails, don't cache — the create/edit call will surface
        # the real error, and we retry the listing next time.
        return set()
    _repo_labels_cache[repo] = names
    return names


async def ensure_labels(repo: str, labels: list[str] | None) -> None:
    """Create any missing ``area:*`` / ``proj:*`` labels in ``repo``.

    The dashboard tags each task with an ``area:<slug>`` / ``proj:<slug>``
    label, but ``gh`` refuses to attach a label that doesn't exist yet — so
    the first task in a brand-new PARA area/project would fail. This creates
    only those two namespaces (never touches labels the user already has, so
    custom colors/descriptions are preserved) and is a no-op once cached.
    """
    if not repo or "/" not in repo:
        return
    wanted = [
        lab for lab in (labels or [])
        if isinstance(lab, str) and (_AREA_LABEL_RE.match(lab) or _PROJ_LABEL_RE.match(lab))
    ]
    if not wanted:
        return
    existing = await _repo_labels(repo)
    for lab in wanted:
        if lab in existing:
            continue
        is_area = bool(_AREA_LABEL_RE.match(lab))
        slug = lab.split(":", 1)[1]
        desc = ("area: " if is_area else "project: ") + slug
        color = _AREA_LABEL_COLOR if is_area else _PROJ_LABEL_COLOR
        try:
            await _gh_cli([
                "label", "create", lab, "--repo", repo,
                "--color", color, "--description", desc,
            ])
            existing.add(lab)
        except Exception:
            # Best-effort — if creation races or fails, the subsequent
            # issue create/edit surfaces the actual error to the user.
            pass


# ── issue-level ops (gh CLI) ────────────────────────────────────


async def add_labels(cfg: TasksConfig, repo: str, number: int, labels: list[str]) -> None:
    if not labels:
        return
    await ensure_labels(repo, labels)
    args = ["issue", "edit", str(number), "--repo", repo]
    for lab in labels:
        args.extend(["--add-label", lab])
    await _gh_cli(args)
    invalidate_caches(project_node_id=await resolve_project_node_id(cfg))


async def remove_labels(cfg: TasksConfig, repo: str, number: int, labels: list[str]) -> None:
    if not labels:
        return
    args = ["issue", "edit", str(number), "--repo", repo]
    for lab in labels:
        args.extend(["--remove-label", lab])
    await _gh_cli(args)
    invalidate_caches(project_node_id=await resolve_project_node_id(cfg))


async def edit_labels(
    cfg: TasksConfig, repo: str, number: int,
    *, add: list[str] | None = None, remove: list[str] | None = None,
) -> None:
    """One ``gh issue edit`` call that combines add + remove labels.

    Avoids the two-subprocess overhead of calling ``add_labels`` and then
    ``remove_labels`` back-to-back when both lists are non-empty (typical
    on a "move task between areas/projects" PATCH). Falls back to the
    appropriate single-direction call when only one side has work.
    """
    add = add or []
    remove = remove or []
    if not add and not remove:
        return
    await ensure_labels(repo, add)
    args = ["issue", "edit", str(number), "--repo", repo]
    for lab in add:
        args.extend(["--add-label", lab])
    for lab in remove:
        args.extend(["--remove-label", lab])
    await _gh_cli(args)
    invalidate_caches(project_node_id=await resolve_project_node_id(cfg))


async def edit_issue(
    cfg: TasksConfig,
    repo: str,
    number: int,
    *,
    title: str | None = None,
    body: str | None = None,
) -> None:
    if title is None and body is None:
        return
    args = ["issue", "edit", str(number), "--repo", repo]
    if title is not None:
        args.extend(["--title", title])
    if body is not None:
        args.extend(["--body", body])
    await _gh_cli(args)
    invalidate_caches(project_node_id=await resolve_project_node_id(cfg))


async def close_issue(
    cfg: TasksConfig,
    repo: str,
    number: int,
    reason: Literal["completed", "not_planned"] = "completed",
) -> None:
    # The python-side identifier is underscore-style for ergonomics, but the
    # gh CLI's --reason flag expects `not planned` (with a space) — passing
    # `not_planned` makes gh fail with
    #   invalid argument "not_planned" for "-r, --reason" flag
    # Translate at the boundary so callers keep the typed enum.
    gh_reason = "not planned" if reason == "not_planned" else reason
    args = ["issue", "close", str(number), "--repo", repo, "--reason", gh_reason]
    await _gh_cli(args)
    invalidate_caches(project_node_id=await resolve_project_node_id(cfg))


async def reopen_issue(cfg: TasksConfig, repo: str, number: int) -> None:
    args = ["issue", "reopen", str(number), "--repo", repo]
    await _gh_cli(args)
    invalidate_caches(project_node_id=await resolve_project_node_id(cfg))


# ── label classification (exported) ─────────────────────────────


def classify_labels(labels: list[str]) -> tuple[str | None, str | None, list[str]]:
    return _classify_labels(labels)


def area_label(slug: str) -> str:
    return f"area:{slug}"


def project_label(slug: str) -> str:
    return f"proj:{slug}"
