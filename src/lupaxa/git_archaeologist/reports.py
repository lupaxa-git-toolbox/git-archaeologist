"""Shared history facts and command reports.

Renderers format these dictionaries. They do not recompute metrics.
"""

from __future__ import annotations

import base64
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from lupaxa.git_archaeologist.errors import ToolError
from lupaxa.git_archaeologist.gitio import Change, Commit, Leaf, Repository, WarningNote
from lupaxa.git_archaeologist.options import Request
from lupaxa.git_archaeologist.text import (
    auto_group,
    bucket_key,
    bucket_range,
    elapsed_seconds,
    exclusive_bucket_end,
    extension_of,
    format_duration,
    from_unix,
    language_of,
    path_record,
    percentage,
)
from lupaxa.git_archaeologist.version import get_version

_MERGE_LABEL = (
    "Merge commits count toward activity. File events include only changes that differ "
    "from every parent."
)
_DAY = 86400
_LFS_VERSION = b"version https://git-lfs.github.com/spec/v1\n"


@dataclass
class _Origin:
    status: str
    commit: str
    path: bytes


@dataclass
class _Trace:
    status: str
    origins: list[_Origin]
    nodes: list[tuple[str, bytes]]
    rename_count: int
    keys: list[str]


class Analysis:
    """Facts for one resolved target."""

    def __init__(self, repo: Repository, request: Request, target: str) -> None:
        self.repo = repo
        self.request = request
        self.target = target
        self.renames = not request.no_renames
        self._maps: dict[str, dict[bytes, Leaf]] = {}
        self._traces: dict[tuple[str, bytes, bool], _Trace] = {}
        self._events: dict[str, list[Change]] | None = None
        self._authors: dict[str, frozenset[tuple[str, str]]] = {}

    def scope_path(self) -> bytes | None:
        """Directory or file limit. ``history`` uses ``path`` as its subject instead."""
        if self.request.command == "history":
            return None
        return self.request.path

    def selected(self) -> list[Commit]:
        """Reachable commits inside the revision range and committer-time window."""
        allowed = self._revision_allowance()
        chosen: list[Commit] = []
        for oid in self.repo.order:
            if allowed is not None and oid not in allowed:
                continue
            commit = self.repo.commits[oid]
            if self.request.since is not None and commit.committer_ts < self.request.since:
                continue
            if self.request.until is not None and commit.committer_ts >= self.request.until:
                continue
            chosen.append(commit)
        return chosen

    def _revision_allowance(self) -> set[str] | None:
        since_oid = self.request.since_rev_oid
        until_oid = self.request.until_rev_oid
        if since_oid is None and until_oid is None:
            return None
        if until_oid is None:
            allowed = set(self.repo.commits)
        else:
            allowed = set(self.repo.ancestor_ids(until_oid))
        if since_oid is not None:
            allowed -= self.repo.ancestor_ids(since_oid)
        return allowed

    def path_map(self, oid: str) -> dict[bytes, Leaf]:
        """Leaf index for one commit, cached by tree ID."""
        tree = self.repo.commits[oid].tree
        cached = self._maps.get(tree)
        if cached is None:
            cached = {leaf.path: leaf for leaf in self.repo.tree_leaves(tree)}
            self._maps[tree] = cached
        return cached

    def target_map(self) -> dict[bytes, Leaf]:
        return self.path_map(self.target)

    def warm_trees(self) -> None:
        commits = list(self.repo.commits.values())
        total = len(commits)
        for index, commit in enumerate(commits, start=1):
            self.repo.note_progress("Reading trees", index, total)
            self.path_map(commit.oid)

    def event_index(self) -> dict[str, list[Change]]:
        if self._events is None:
            grouped: dict[str, list[Change]] = defaultdict(list)
            for change in self.repo.changes:
                grouped[change.commit].append(change)
            self._events = grouped
        return self._events

    def activity_span(self, commits: list[Commit]) -> tuple[Commit | None, Commit | None]:
        """Earliest and latest committer timestamps. The full ID breaks ties."""
        if not commits:
            return None, None
        first = min(commits, key=lambda item: (item.committer_ts, item.oid))
        last = max(commits, key=lambda item: (item.committer_ts, item.oid))
        return first, last

    def canonical_author(self, commit: Commit) -> tuple[str, str]:
        return self.repo.canonicalize(commit.author_name, commit.author_email)

    def trace(self, oid: str, path: bytes) -> _Trace:
        """Follow one path backward. Rename edges connect only when enabled."""
        key = (oid, path, self.renames)
        cached = self._traces.get(key)
        if cached is not None:
            return cached
        origins: list[_Origin] = []
        nodes: list[tuple[str, bytes]] = []
        renames = 0
        seen: set[tuple[str, bytes]] = set()

        def walk(commit_oid: str, current: bytes) -> None:
            nonlocal renames
            marker = (commit_oid, current)
            if marker in seen or commit_oid not in self.repo.commits:
                return
            seen.add(marker)
            nodes.append(marker)
            commit = self.repo.commits[commit_oid]
            parents = [parent for parent in commit.parents if parent in self.repo.commits]
            if commit_oid in self.repo.shallow_boundaries:
                origins.append(_Origin("boundary", commit_oid, current))
                return
            if not parents:
                origins.append(_Origin("confirmed", commit_oid, current))
                return
            if len(commit.parents) > 1:
                holders = [parent for parent in parents if current in self.path_map(parent)]
                if len(holders) != 1:
                    origins.append(_Origin("ambiguous", commit_oid, current))
                    for holder in holders:
                        walk(holder, current)
                    return
                walk(holders[0], current)
                return
            parent = parents[0]
            event = self._incoming(commit_oid, current)
            if event is None:
                walk(parent, current)
                return
            if event.status in {"A", "C"}:
                origins.append(_Origin("confirmed", commit_oid, current))
                return
            if event.status == "R" and event.old_path is not None and self.renames:
                renames += 1
                walk(parent, event.old_path)
                return
            if event.status == "R":
                origins.append(_Origin("confirmed", commit_oid, current))
                return
            walk(parent, current)

        walk(oid, path)
        status = _origin_status(origins)
        keys = sorted(
            f"{item.commit}:{base64.b64encode(item.path).decode('ascii')}" for item in origins
        )
        traced = _Trace(status, origins, nodes, renames, keys)
        self._traces[key] = traced
        return traced

    def _incoming(self, oid: str, path: bytes) -> Change | None:
        for event in self.event_index().get(oid, []):
            if event.new_path == path:
                return event
        return None

    def touches(self, traced: _Trace) -> list[Change]:
        """Eligible changes on this identity, without double-counting a commit."""
        found: list[Change] = []
        seen: set[str] = set()
        paths = {path for _oid, path in traced.nodes}
        node_commits = {oid for oid, _path in traced.nodes}
        for oid in node_commits:
            for event in self.event_index().get(oid, []):
                if event.commit in seen:
                    continue
                if event.new_path in paths or event.old_path in paths:
                    found.append(event)
                    seen.add(event.commit)
        return found

    def limit_of(self, default: int) -> int:
        if self.request.limit_explicit:
            return self.request.limit
        return default

    def slice_rows(self, rows: list[Any], default: int) -> dict[str, Any]:
        limit = self.limit_of(default)
        total = len(rows)
        shown = rows if limit == 0 else rows[:limit]
        return {
            "total": total,
            "returned": len(shown),
            "truncated": total > len(shown),
            "rows": shown,
        }


def _origin_status(origins: list[_Origin]) -> str:
    if not origins:
        return "ambiguous"
    if (
        any(item.status == "ambiguous" for item in origins)
        or len({item.commit for item in origins}) > 1
    ):
        return "ambiguous"
    return origins[0].status


def _identity_key(traced: _Trace) -> str:
    if traced.keys:
        return (
            traced.keys[0] if traced.status != "ambiguous" else "ambiguous:" + "|".join(traced.keys)
        )
    return "unknown"


def build_report(repo: Repository, request: Request, target: str) -> dict[str, Any]:
    """Build the command report dictionary."""
    analysis = Analysis(repo, request, target)
    repo.rename_threshold = request.rename_threshold
    repo.detect_copies = request.copies and not request.no_renames
    repo.load_mailmap(repo.commits[target].tree, enabled=not request.no_mailmap)
    needs_changes = request.command in {
        "timeline",
        "dig",
        "survivors",
        "extinct",
        "files",
        "history",
        "churn",
        "evolution",
    } or (request.command == "contributors" and request.path is not None)
    if needs_changes and not (request.command == "dig" and request.quick):
        repo.ensure_changes(renames=not request.no_renames)
    if request.command in {
        "extinct",
        "blobs",
        "evolution",
        "history",
        "files",
        "survivors",
        "churn",
    } or (request.command == "dig" and not request.quick):
        analysis.warm_trees()
    if request.command == "dig" and request.quick:
        analysis.path_map(target)
    builders = {
        "dig": _dig,
        "timeline": _timeline,
        "contributors": _contributors,
        "survivors": _survivors,
        "extinct": _extinct,
        "files": _files,
        "history": _history,
        "churn": _churn,
        "blobs": _blobs,
        "evolution": _evolution,
    }
    repo.note_progress(f"Analysing {request.command}")
    data, sections = builders[request.command](analysis)
    status = "ok"
    if any(item.get("status") == "failed" for item in sections.values()):
        status = "partial"
    return _envelope(repo, request, target, analysis, data, sections, status)


def _envelope(
    repo: Repository,
    request: Request,
    target: str,
    analysis: Analysis,
    data: dict[str, Any],
    sections: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    reachable = list(repo.commits.values())
    selected = analysis.selected()
    first_r, last_r = analysis.activity_span(reachable)
    head_state, head_branch, head_oid = repo.resolve_head()
    branch_count, tag_count = repo.local_ref_counts()
    selector_kind = "head"
    selector_value = "HEAD"
    if request.branch:
        selector_kind = "branch"
        selector_value = request.branch
    elif request.revision:
        selector_kind = "revision"
        selector_value = request.revision
    warnings = [_warning_dict(item) for item in _sorted_warnings(repo.warnings)]
    repository = {
        "bare": repo.bare,
        "common_dir": str(repo.common_dir),
        "display_name": repo.display_name,
        "git_dir": str(repo.git_dir),
        "head": {"branch": head_branch, "oid": head_oid, "state": head_state},
        "local_branch_count": branch_count,
        "local_tag_count": tag_count,
        "object_format": repo.object_format(target),
        "repository_state": "normal",
        "root": None if repo.root is None else str(repo.root),
        "shallow": repo.shallow,
    }
    dated = request.since is not None or request.until is not None
    ranged = request.since_rev is not None or request.until_rev is not None
    if dated and ranged:
        window = "revision_and_time"
    elif ranged:
        window = "revision"
    elif dated:
        window = "committer_time"
    else:
        window = "all_reachable"
    scope = {
        "path": request.path_text,
        "reachable_commits": len(reachable),
        "selected_commits": len(selected),
        "selector_kind": selector_kind,
        "selector_value": selector_value,
        "since": from_unix(request.since) if request.since is not None else None,
        "since_rev": request.since_rev,
        "target_id": target,
        "target_versus_window": (
            "Revision and date filters select activity events. "
            "The target tree remains the resolved commit."
        ),
        "until": from_unix(request.until) if request.until is not None else None,
        "until_rev": request.until_rev,
        "window": window,
    }
    meta = {
        "first_reachable_at": None if first_r is None else from_unix(first_r.committer_ts),
        "history_complete": not repo.shallow,
        "last_reachable_at": None if last_r is None else from_unix(last_r.committer_ts),
        "mailmap": {"source": repo.mailmap_source, "status": repo.mailmap_status},
        "merge_policy": "merge_only_file_changes",
        "merge_policy_label": _MERGE_LABEL,
        "renames": {
            "complete": repo.rename_complete,
            "copies": request.copies and not request.no_renames,
            "enabled": not request.no_renames,
            "limit": None if request.no_renames else 1000,
            "threshold": None if request.no_renames else request.rename_threshold,
        },
        "sections": sections,
    }
    return {
        "analysis": meta,
        "command": request.command,
        "data": data,
        "errors": [],
        "repository": repository,
        "schema_version": "1.0",
        "scope": scope,
        "status": status,
        "tool_version": get_version(),
        "warnings": warnings,
    }


def _warning_dict(note: WarningNote) -> dict[str, Any]:
    return {"code": note.code, "context": note.context, "message": note.message}


def _sorted_warnings(notes: list[WarningNote]) -> list[WarningNote]:
    unique: dict[tuple[str, str], WarningNote] = {}
    for note in notes:
        key = (note.code, str(sorted(note.context.items())))
        unique.setdefault(key, note)
    return sorted(unique.values(), key=lambda item: (item.code, str(sorted(item.context.items()))))


def _commit_ref(commit: Commit | None) -> dict[str, Any] | None:
    if commit is None:
        return None
    return {"id": commit.oid, "committer_at": from_unix(commit.committer_ts)}


def _skipped(reason: str) -> dict[str, Any]:
    return {"reason": reason, "rows": None, "status": "skipped"}


def _complete(payload: dict[str, Any]) -> dict[str, Any]:
    payload["status"] = "complete"
    return payload


def _dig(analysis: Analysis) -> tuple[dict[str, Any], dict[str, Any]]:
    request = analysis.request
    reachable = list(analysis.repo.commits.values())
    selected = analysis.selected()
    first_s, last_s = analysis.activity_span(selected)
    first_r, last_r = analysis.activity_span(reachable)
    authors = {analysis.canonical_author(item) for item in selected}
    target_leaves = analysis.target_map()
    summary = {
        "accessible_span": {
            "first": _commit_ref(first_r),
            "last": _commit_ref(last_r),
            "seconds": elapsed_seconds(
                None if first_r is None else first_r.committer_ts,
                None if last_r is None else last_r.committer_ts,
            ),
        },
        "canonical_author_count": len(authors),
        "selected_activity": {"first": _commit_ref(first_s), "last": _commit_ref(last_s)},
        "selected_commits": len(selected),
        "target_entries": len(target_leaves),
    }
    sections: dict[str, Any] = {"repository": {"status": "complete"}}
    if request.quick:
        analysis.repo.warnings.append(
            WarningNote(
                "SKIPPED_QUICK",
                "Quick mode skipped rename, lifecycle, churn, extinct, and historical blob scans.",
                {},
            )
        )
        gaps = _gap_rows(selected)
        data = {
            "blobs": _skipped("quick_mode"),
            "discoveries": {
                "extinct_episodes": _skipped("quick_mode"),
                "gaps": _complete(analysis.slice_rows(gaps, 5)),
                "historical_blobs": _skipped("quick_mode"),
                "status": "complete",
            },
            "empty_selection": _empty_selection(request, selected),
            "extinct": _skipped("quick_mode"),
            "most_changed": _skipped("quick_mode"),
            "repository_summary": summary,
            "survivors": _skipped("quick_mode"),
        }
        for name in ("survivors", "most_changed", "extinct", "blobs"):
            sections[name] = {"reason": "quick_mode", "status": "skipped"}
        sections["discoveries"] = {"status": "complete"}
        return data, sections
    survivor_rows = _survivor_rows(analysis)
    changed_rows = _file_rows(analysis)
    changed_rows.sort(key=lambda row: (-row["window_touches"], row["path_bytes"]))
    extinct = _extinct(analysis)[0]
    blob_data = _blobs(analysis)[0]
    episodes = _extinct_episodes(analysis)
    historical = [
        row
        for row in blob_data["rows"]
        if row["presence"] == "historical_only" and row["size"] >= 10 * 1024 * 1024
    ]
    data = {
        "blobs": _complete(
            analysis.slice_rows(
                [row for row in blob_data["rows"] if row["size"] >= 10 * 1024 * 1024],
                5,
            )
        ),
        "discoveries": {
            "extinct_episodes": _complete(analysis.slice_rows(episodes, 5)),
            "gaps": _complete(analysis.slice_rows(_gap_rows(selected), 5)),
            "historical_blobs": _complete(analysis.slice_rows(historical, 5)),
            "status": "complete",
        },
        "empty_selection": _empty_selection(request, selected),
        "extinct": {
            "absent_directories": extinct["absent_directories"],
            "absent_paths": extinct["absent_paths"],
            "sample": analysis.slice_rows(extinct["rows"], 5)["rows"],
            "status": "complete",
        },
        "most_changed": _complete(
            analysis.slice_rows(_public_file_rows(changed_rows, use_window=False), 5)
        ),
        "repository_summary": summary,
        "survivors": _complete(analysis.slice_rows(_public_survivor_rows(survivor_rows), 5)),
    }
    for name in ("survivors", "most_changed", "extinct", "blobs", "discoveries"):
        sections[name] = {"status": "complete"}
    return data, sections


def _activity_filtered(request: Request) -> bool:
    return (
        request.since is not None
        or request.until is not None
        or request.since_rev is not None
        or request.until_rev is not None
    )


def _path_in_scope(path: bytes | None, scope: bytes | None) -> bool:
    if scope is None:
        return True
    if path is None:
        return False
    return path == scope or path.startswith(scope + b"/")


def _event_in_scope(event: Change, scope: bytes | None) -> bool:
    return _path_in_scope(event.new_path, scope) or _path_in_scope(event.old_path, scope)


def _empty_selection(request: Request, selected: list[Commit]) -> str | None:
    if selected:
        return None
    ranged = request.since_rev is not None or request.until_rev is not None
    dated = request.since is not None or request.until is not None
    if ranged and dated:
        return "No selected commits fall in the requested revision range and time interval."
    if ranged:
        return "No selected commits fall in the requested revision range."
    if dated:
        return "No selected commits fall in the requested committer-time interval."
    return None


def _gap_rows(commits: list[Commit]) -> list[dict[str, Any]]:
    ordered = sorted(commits, key=lambda item: (item.committer_ts, item.oid))
    rows: list[dict[str, Any]] = []
    previous: Commit | None = None
    seen_ts: int | None = None
    for commit in ordered:
        if seen_ts == commit.committer_ts:
            continue
        if previous is not None:
            seconds = commit.committer_ts - previous.committer_ts
            if seconds >= 90 * _DAY:
                rows.append(
                    {
                        "duration_seconds": seconds,
                        "duration": format_duration(seconds),
                        "from": _commit_ref(previous),
                        "to": _commit_ref(commit),
                    }
                )
        previous = commit
        seen_ts = commit.committer_ts
    rows.sort(key=lambda row: (-row["duration_seconds"], row["to"]["id"]))
    return rows


def _contributors(analysis: Analysis) -> tuple[dict[str, Any], dict[str, Any]]:
    selected = analysis.selected()
    scope = analysis.scope_path()
    if scope is None:
        counted = selected
    else:
        touching = {
            event.commit for event in analysis.repo.changes if _event_in_scope(event, scope)
        }
        counted = [commit for commit in selected if commit.oid in touching]
    selected_ids = {item.oid for item in counted}
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for commit in analysis.repo.commits.values():
        raw = (commit.author_name, commit.author_email)
        canonical = analysis.canonical_author(commit)
        bucket = grouped.get(canonical)
        if bucket is None:
            bucket = {
                "active_months": set(),
                "aliases": set(),
                "canonical_email": canonical[1],
                "canonical_name": canonical[0],
                "commits": 0,
                "first_lifetime": commit,
                "first_selected": None,
                "last_lifetime": commit,
                "last_selected": None,
            }
            grouped[canonical] = bucket
        bucket["aliases"].add(raw)
        if _earlier(commit, bucket["first_lifetime"]):
            bucket["first_lifetime"] = commit
        if _later(commit, bucket["last_lifetime"]):
            bucket["last_lifetime"] = commit
        if commit.oid not in selected_ids:
            continue
        bucket["commits"] += 1
        bucket["active_months"].add(from_unix(commit.committer_ts)[:7])
        if bucket["first_selected"] is None or _earlier(commit, bucket["first_selected"]):
            bucket["first_selected"] = commit
        if bucket["last_selected"] is None or _later(commit, bucket["last_selected"]):
            bucket["last_selected"] = commit
    rows = [bucket for bucket in grouped.values() if bucket["commits"] > 0]
    total_commits = len(counted)

    def sort_key(row: dict[str, Any]) -> tuple[Any, ...]:
        identity = (
            row["canonical_name"].encode("utf-8", "surrogateescape"),
            row["canonical_email"].encode("utf-8", "surrogateescape"),
        )
        mode = analysis.request.sort or "commits"
        if mode == "first":
            return (row["first_selected"].committer_ts, row["first_selected"].oid, identity)
        if mode == "last":
            return (-row["last_selected"].committer_ts, row["last_selected"].oid, identity)
        if mode == "name":
            return (tuple(map(ord, row["canonical_name"])), identity)
        return (-row["commits"], identity)

    rows.sort(key=sort_key)
    public = []
    for row in rows:
        aliases = sorted(
            row["aliases"],
            key=lambda item: (
                item[0].encode("utf-8", "surrogateescape"),
                item[1].encode("utf-8", "surrogateescape"),
            ),
        )
        public.append(
            {
                "active_months": len(row["active_months"]),
                "aliases": [{"email": email, "name": name} for name, email in aliases],
                "canonical_email": row["canonical_email"],
                "canonical_name": row["canonical_name"],
                "commits": row["commits"],
                "first_lifetime": _commit_ref(row["first_lifetime"]),
                "first_selected": _commit_ref(row["first_selected"]),
                "last_lifetime": _commit_ref(row["last_lifetime"]),
                "last_selected": _commit_ref(row["last_selected"]),
                "percentage": percentage(row["commits"], total_commits),
            }
        )
    payload = analysis.slice_rows(public, 20)
    payload["selected_commits"] = total_commits
    payload["empty_selection"] = _empty_selection(analysis.request, counted)
    return _complete(payload), {"contributors": {"status": "complete"}}


def _earlier(left: Commit, right: Commit) -> bool:
    return (left.committer_ts, left.oid) < (right.committer_ts, right.oid)


def _later(left: Commit, right: Commit) -> bool:
    return (left.committer_ts, left.oid) > (right.committer_ts, right.oid)


def _timeline(analysis: Analysis) -> tuple[dict[str, Any], dict[str, Any]]:
    selected = analysis.selected()
    scope = analysis.scope_path()
    if scope is None:
        visible = selected
    else:
        touching = {
            event.commit for event in analysis.repo.changes if _event_in_scope(event, scope)
        }
        visible = [commit for commit in selected if commit.oid in touching]
    visible_ids = {item.oid for item in visible}
    first, last = analysis.activity_span(visible)
    group = analysis.request.group
    if group == "auto":
        span = None if first is None or last is None else last.committer_ts - first.committer_ts
        group = auto_group(span)
    buckets: dict[str, dict[str, Any]] = {}
    if first is not None and last is not None:
        for key in bucket_range(first.committer_ts, last.committer_ts, group):
            buckets[key] = _empty_bucket(key)
    for commit in visible:
        key = bucket_key(commit.committer_ts, group)
        bucket = buckets[key]
        bucket["commits"] += 1
        if len(commit.parents) > 1:
            bucket["merges"] += 1
        bucket["authors"].add(analysis.canonical_author(commit))
    for event in analysis.repo.changes:
        if event.commit not in visible_ids or not _event_in_scope(event, scope):
            continue
        commit = analysis.repo.commits[event.commit]
        bucket = buckets[bucket_key(commit.committer_ts, group)]
        _add_event(bucket, event)
    rows = []
    for key in buckets:
        bucket = buckets[key]
        rows.append(_finish_bucket(bucket))
    totals = _empty_bucket("total")
    for commit in visible:
        totals["commits"] += 1
        if len(commit.parents) > 1:
            totals["merges"] += 1
        totals["authors"].add(analysis.canonical_author(commit))
    for event in analysis.repo.changes:
        if event.commit in visible_ids and _event_in_scope(event, scope):
            _add_event(totals, event)
    return (
        {
            "buckets": rows,
            "empty_selection": _empty_selection(analysis.request, visible),
            "group": group,
            "status": "complete",
            "totals": _finish_bucket(totals),
        },
        {"timeline": {"status": "complete"}},
    )


def _empty_bucket(key: str) -> dict[str, Any]:
    return {
        "added": 0,
        "additions": 0,
        "authors": set(),
        "binary_events": 0,
        "churn": 0,
        "commits": 0,
        "copies": 0,
        "deleted": 0,
        "deletions": 0,
        "key": key,
        "merges": 0,
        "modified": 0,
        "renames": 0,
        "type_changed": 0,
    }


def _add_event(bucket: dict[str, Any], event: Change) -> None:
    if event.status == "A":
        bucket["added"] += 1
    elif event.status == "D":
        bucket["deleted"] += 1
    elif event.status == "T":
        bucket["type_changed"] += 1
    elif event.status == "R":
        bucket["renames"] += 1
    elif event.status == "C":
        bucket["copies"] += 1
    else:
        bucket["modified"] += 1
    if event.binary:
        bucket["binary_events"] += 1
        return
    additions = event.additions or 0
    deletions = event.deletions or 0
    bucket["additions"] += additions
    bucket["deletions"] += deletions
    bucket["churn"] += additions + deletions


def _finish_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    finished = dict(bucket)
    finished["authors"] = len(bucket["authors"])
    return finished


def _survivor_rows(analysis: Analysis) -> list[dict[str, Any]]:
    rows = []
    latest = analysis.activity_span(list(analysis.repo.commits.values()))[1]
    latest_ts = None if latest is None else latest.committer_ts
    scope = analysis.scope_path()
    for path, leaf in analysis.target_map().items():
        if not _path_in_scope(path, scope):
            continue
        traced = analysis.trace(analysis.target, path)
        rows.append(_presence_row(analysis, path, leaf, traced, latest_ts))
    mode = analysis.request.sort or "age"
    if mode == "commits":
        rows.sort(key=lambda row: (-row["rank_touches"], row["path_bytes"]))
    elif mode == "path":
        rows.sort(key=lambda row: row["path_bytes"])
    else:
        rows.sort(
            key=lambda row: (row["earliest_ts"] is None, row["earliest_ts"] or 0, row["path_bytes"])
        )
    return rows


def _presence_row(
    analysis: Analysis,
    path: bytes,
    leaf: Leaf | None,
    traced: _Trace,
    latest_ts: int | None,
) -> dict[str, Any]:
    if traced.status == "ambiguous":
        analysis.repo.warnings.append(
            WarningNote(
                "AMBIGUOUS_ORIGIN",
                "At least one path has more than one possible origin. No single birth was chosen.",
                {},
            )
        )
    nodes = traced.nodes
    observed = [analysis.repo.commits[oid] for oid, _path in nodes]
    earliest = min(observed, key=lambda item: (item.committer_ts, item.oid)) if observed else None
    birth = None
    if traced.status == "confirmed" and traced.origins:
        birth = analysis.repo.commits[traced.origins[0].commit]
    events = analysis.touches(traced)
    selected_ids = {item.oid for item in analysis.selected()}
    window_events = [event for event in events if event.commit in selected_ids]
    lifetime_authors = {
        analysis.canonical_author(analysis.repo.commits[event.commit]) for event in events
    }
    window_authors = {
        analysis.canonical_author(analysis.repo.commits[event.commit]) for event in window_events
    }
    additions = sum(event.additions or 0 for event in events if not event.binary)
    deletions = sum(event.deletions or 0 for event in events if not event.binary)
    window_additions = sum(event.additions or 0 for event in window_events if not event.binary)
    window_deletions = sum(event.deletions or 0 for event in window_events if not event.binary)
    use_window = _activity_filtered(analysis.request)
    rank_touches = len(window_events) if use_window else len(events)
    size = None
    if leaf is not None and leaf.kind != "gitlink":
        size = analysis.repo.blob_sizes([leaf.oid]).get(leaf.oid)
    paths = sorted({item_path for _oid, item_path in nodes})
    return {
        "binary_events": sum(1 for event in events if event.binary),
        "confirmed_birth": _commit_ref(birth),
        "current": None
        if leaf is None
        else {
            "kind": leaf.kind,
            "mode": leaf.mode,
            "object_id": leaf.oid,
            "size": size,
        },
        "earliest_observed": _commit_ref(earliest),
        "earliest_ts": None if earliest is None else earliest.committer_ts,
        "identity_keys": traced.keys,
        "inferred_renames": traced.rename_count,
        "known_paths": [path_record(item) for item in paths],
        "lifetime_churn": additions + deletions,
        "lifetime_contributors": len(lifetime_authors),
        "lifetime_touches": len(events),
        "origin_status": traced.status,
        "path_bytes": path,
        "rank_touches": rank_touches,
        "window_binary_events": sum(1 for event in window_events if event.binary),
        "window_churn": window_additions + window_deletions,
        "window_contributors": len(window_authors),
        "window_additions": window_additions,
        "window_deletions": window_deletions,
        "lifetime_additions": additions,
        "lifetime_deletions": deletions,
        "window_touches": len(window_events),
        "lifetime_seconds": elapsed_seconds(
            None if earliest is None else earliest.committer_ts, latest_ts
        ),
    }


def _public_survivor_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    public = []
    for row in rows:
        public.append(
            {
                "confirmed_birth": row["confirmed_birth"],
                "current": row["current"],
                "earliest_observed": row["earliest_observed"],
                "identity_keys": row["identity_keys"],
                "inferred_renames": row["inferred_renames"],
                "known_paths": row["known_paths"],
                "lifetime_contributors": row["lifetime_contributors"],
                "lifetime_seconds": row["lifetime_seconds"],
                "lifetime_duration": format_duration(row["lifetime_seconds"]),
                "lifetime_touches": row["lifetime_touches"],
                "origin_status": row["origin_status"],
                "path": path_record(row["path_bytes"]),
                "window_contributors": row["window_contributors"],
                "window_touches": row["window_touches"],
            }
        )
    return public


def _survivors(analysis: Analysis) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = _public_survivor_rows(_survivor_rows(analysis))
    payload = analysis.slice_rows(rows, 20)
    payload["empty_selection"] = _empty_selection(analysis.request, analysis.selected())
    return _complete(payload), {"survivors": {"status": "complete"}}


def _file_rows(analysis: Analysis) -> list[dict[str, Any]]:
    latest = analysis.activity_span(list(analysis.repo.commits.values()))[1]
    latest_ts = None if latest is None else latest.committer_ts
    rows = []
    scope = analysis.scope_path()
    for path, leaf in analysis.target_map().items():
        if not _path_in_scope(path, scope):
            continue
        traced = analysis.trace(analysis.target, path)
        rows.append(_presence_row(analysis, path, leaf, traced, latest_ts))
    return rows


def _public_file_rows(
    rows: list[dict[str, Any]], *, use_window: bool = False
) -> list[dict[str, Any]]:
    public = []
    for row in rows:
        public.append(
            {
                "binary_events": row["window_binary_events"]
                if use_window
                else row["binary_events"],
                "churn": row["window_churn"] if use_window else row["lifetime_churn"],
                "confirmed_birth": row["confirmed_birth"],
                "contributors": row["window_contributors"]
                if use_window
                else row["lifetime_contributors"],
                "current": row["current"],
                "earliest_observed": row["earliest_observed"],
                "identity_keys": row["identity_keys"],
                "inferred_renames": row["inferred_renames"],
                "lifetime_additions": row["lifetime_additions"],
                "lifetime_churn": row["lifetime_churn"],
                "lifetime_contributors": row["lifetime_contributors"],
                "lifetime_deletions": row["lifetime_deletions"],
                "lifetime_touches": row["lifetime_touches"],
                "origin_status": row["origin_status"],
                "path": path_record(row["path_bytes"]),
                "touches": row["window_touches"] if use_window else row["lifetime_touches"],
                "window_additions": row["window_additions"],
                "window_churn": row["window_churn"],
                "window_contributors": row["window_contributors"],
                "window_deletions": row["window_deletions"],
                "window_touches": row["window_touches"],
            }
        )
    return public


def _files(analysis: Analysis) -> tuple[dict[str, Any], dict[str, Any]]:
    rows = _file_rows(analysis)
    use_window = _activity_filtered(analysis.request)
    mode = analysis.request.sort or "commits"

    def key(row: dict[str, Any]) -> tuple[Any, ...]:
        path = row["path_bytes"]
        touches = row["window_touches"] if use_window else row["lifetime_touches"]
        contributors = row["window_contributors"] if use_window else row["lifetime_contributors"]
        churn = row["window_churn"] if use_window else row["lifetime_churn"]
        size = None if row["current"] is None else row["current"]["size"]
        if mode == "age":
            return (row["earliest_ts"] is None, row["earliest_ts"] or 0, path)
        if mode == "path":
            return (path,)
        if mode == "contributors":
            return (-contributors, path)
        if mode == "churn":
            return (-churn, path)
        if mode == "size":
            return (size is None, -(size or 0), path)
        return (-touches, path)

    rows.sort(key=key)
    public = [_public_file_rows([row], use_window=use_window)[0] for row in rows]
    payload = analysis.slice_rows(public, 20)
    payload["metric_basis"] = "window" if use_window else "lifetime"
    payload["empty_selection"] = _empty_selection(analysis.request, analysis.selected())
    return _complete(payload), {"files": {"status": "complete"}}


def _all_paths(analysis: Analysis) -> dict[bytes, list[str]]:
    found: dict[bytes, list[str]] = defaultdict(list)
    for commit in analysis.repo.commits.values():
        for path in analysis.path_map(commit.oid):
            found[path].append(commit.oid)
    return found


def _extinct(analysis: Analysis) -> tuple[dict[str, Any], dict[str, Any]]:
    historical = _all_paths(analysis)
    target_paths = set(analysis.target_map())
    scope = analysis.scope_path()
    absent = [
        path for path in historical if path not in target_paths and _path_in_scope(path, scope)
    ]
    file_rows = [_extinct_file(analysis, path, historical[path]) for path in absent]
    directories = [
        row
        for row in _extinct_directories(analysis, historical, target_paths)
        if _path_in_scope(row["path_bytes"], scope)
    ]
    kind = analysis.request.kind or "all"
    rows: list[dict[str, Any]] = []
    if kind in {"files", "all"}:
        rows.extend(file_rows)
    if kind in {"directories", "all"}:
        rows.extend(directories)
    rows.sort(key=lambda row: _extinct_sort(row, analysis.request.sort or "last"))
    public = [_public_extinct(row) for row in rows]
    payload = analysis.slice_rows(public, 20)
    payload["absent_paths"] = len(file_rows)
    payload["absent_directories"] = len(directories)
    payload["empty_selection"] = _empty_selection(analysis.request, analysis.selected())
    return _complete(payload), {"extinct": {"status": "complete"}}


def _extinct_sort(row: dict[str, Any], mode: str) -> tuple[Any, ...]:
    path = row["path_bytes"]
    if mode == "path":
        return (path,)
    if mode == "commits":
        return (-row["lifetime_touches"], path)
    if mode == "age":
        seconds = row["lifetime_seconds"]
        return (seconds is None, -(seconds or 0), path)
    latest = row["latest_ts"]
    return (-(latest or 0), latest is None, path)


def _extinct_file(analysis: Analysis, path: bytes, commit_ids: list[str]) -> dict[str, Any]:
    commits = [analysis.repo.commits[oid] for oid in commit_ids]
    earliest = min(commits, key=lambda item: (item.committer_ts, item.oid))
    latest = max(commits, key=lambda item: (item.committer_ts, item.oid))
    removals = []
    present = set(commit_ids)
    for commit in analysis.repo.commits.values():
        if commit.oid in present:
            continue
        parents = [parent for parent in commit.parents if parent in present]
        if not parents:
            continue
        event = next(
            (
                item
                for item in analysis.event_index().get(commit.oid, [])
                if item.old_path == path and item.new_path != path
            ),
            None,
        )
        if event is None:
            reason = "unresolved"
            destination = None
            when = commit.committer_ts
        elif event.status == "R":
            reason = "detected_move"
            destination = event.new_path
            when = commit.committer_ts
        elif event.status == "D":
            reason = "observed_deletion"
            destination = None
            when = commit.committer_ts
        else:
            reason = "unresolved"
            destination = None
            when = commit.committer_ts
        removals.append(
            {
                "at": from_unix(when),
                "commit": commit.oid,
                "destination": destination,
                "reason": reason,
                "ts": when,
            }
        )
    reasons = {item["reason"] for item in removals}
    if not reasons:
        absence = "unresolved"
    elif reasons == {"detected_move"}:
        absence = "detected_move"
    elif reasons == {"observed_deletion"}:
        absence = "observed_deletion"
    elif reasons == {"unresolved"}:
        absence = "unresolved"
    else:
        absence = "mixed"
    sizes = []
    oid_map = []
    for oid in commit_ids:
        leaf = analysis.path_map(oid).get(path)
        if leaf and leaf.kind != "gitlink":
            oid_map.append(leaf.oid)
    size_map = analysis.repo.blob_sizes(oid_map)
    sizes = list(size_map.values())
    events = [
        event for event in analysis.repo.changes if event.old_path == path or event.new_path == path
    ]
    selected_ids = {item.oid for item in analysis.selected()}
    authors = {analysis.canonical_author(analysis.repo.commits[event.commit]) for event in events}
    window_events = [event for event in events if event.commit in selected_ids]
    seconds = (
        elapsed_seconds(earliest.committer_ts, latest.committer_ts) if len(removals) == 1 else None
    )
    destinations = []
    for item in removals:
        moved_to = item["destination"]
        if isinstance(moved_to, bytes) and moved_to in analysis.target_map():
            destinations.append(path_record(moved_to))
    return {
        "absence_reason": absence,
        "destinations": destinations,
        "earliest_ts": earliest.committer_ts,
        "first_observed": _commit_ref(earliest),
        "kind": "file",
        "last_observed": _commit_ref(latest),
        "latest_ts": latest.committer_ts,
        "lifetime_contributors": len(authors),
        "lifetime_seconds": seconds,
        "lifetime_touches": len({event.commit for event in events}),
        "max_size": max(sizes) if sizes else None,
        "path_bytes": path,
        "removals": [
            {
                "commit": item["commit"],
                "destination": (
                    path_record(item["destination"])
                    if isinstance(item["destination"], bytes)
                    else None
                ),
                "reason": item["reason"],
                "removed_at": item["at"],
            }
            for item in sorted(removals, key=lambda item: (item["ts"], item["commit"]))
        ],
        "window_touches": len({event.commit for event in window_events}),
    }


def _extinct_directories(
    analysis: Analysis,
    historical: dict[bytes, list[str]],
    target_paths: set[bytes],
) -> list[dict[str, Any]]:
    prefixes: set[bytes] = set()
    for path in historical:
        parts = path.split(b"/")
        for index in range(1, len(parts)):
            prefixes.add(b"/".join(parts[:index]))
    rows = []
    target_prefixes = set()
    for path in target_paths:
        parts = path.split(b"/")
        for index in range(1, len(parts)):
            target_prefixes.add(b"/".join(parts[:index]))
    for prefix in prefixes:
        if prefix in target_prefixes:
            continue
        present_counts: list[tuple[int, str]] = []
        present_commits: list[Commit] = []
        for commit in analysis.repo.commits.values():
            count = sum(
                1
                for leaf_path in analysis.path_map(commit.oid)
                if leaf_path.startswith(prefix + b"/")
            )
            if count:
                present_counts.append((count, commit.oid))
                present_commits.append(commit)
        if not present_commits:
            continue
        earliest = min(present_commits, key=lambda item: (item.committer_ts, item.oid))
        latest = max(present_commits, key=lambda item: (item.committer_ts, item.oid))
        peak = max(count for count, _oid in present_counts)
        events = [
            event
            for event in analysis.repo.changes
            if _path_in_scope(event.old_path, prefix) or _path_in_scope(event.new_path, prefix)
        ]
        authors = {
            analysis.canonical_author(analysis.repo.commits[event.commit]) for event in events
        }
        selected_ids = {item.oid for item in analysis.selected()}
        removals = _directory_removals(analysis, prefix)
        absence = "observed_deletion" if removals else "unresolved"
        rows.append(
            {
                "absence_reason": absence,
                "destinations": [],
                "earliest_ts": earliest.committer_ts,
                "first_observed": _commit_ref(earliest),
                "inferred_from": "tracked_paths",
                "kind": "directory",
                "last_observed": _commit_ref(latest),
                "latest_ts": latest.committer_ts,
                "lifetime_contributors": len(authors),
                "lifetime_seconds": elapsed_seconds(earliest.committer_ts, latest.committer_ts),
                "lifetime_touches": len({event.commit for event in events}),
                "max_size": None,
                "path_bytes": prefix,
                "peak_descendants": peak,
                "removals": removals,
                "window_touches": len(
                    {event.commit for event in events if event.commit in selected_ids}
                ),
            }
        )
    return rows


def _prefix_present(analysis: Analysis, oid: str, prefix: bytes) -> bool:
    needle = prefix + b"/"
    return any(path.startswith(needle) for path in analysis.path_map(oid))


def _directory_removals(analysis: Analysis, prefix: bytes) -> list[dict[str, Any]]:
    """Commits whose tree dropped a directory that a parent still contained."""
    found = []
    for commit in analysis.repo.commits.values():
        if _prefix_present(analysis, commit.oid, prefix):
            continue
        parents = [
            parent
            for parent in commit.parents
            if parent in analysis.repo.commits and _prefix_present(analysis, parent, prefix)
        ]
        if not parents:
            continue
        found.append(
            {
                "commit": commit.oid,
                "destination": None,
                "reason": "observed_deletion",
                "removed_at": from_unix(commit.committer_ts),
                "ts": commit.committer_ts,
            }
        )
    found.sort(key=lambda item: (item["ts"], item["commit"]))
    for item in found:
        del item["ts"]
    return found


def _public_extinct(row: dict[str, Any]) -> dict[str, Any]:
    public = {
        "absence_reason": row["absence_reason"],
        "destinations": row["destinations"],
        "first_observed": row["first_observed"],
        "kind": row["kind"],
        "last_observed": row["last_observed"],
        "lifetime_contributors": row["lifetime_contributors"],
        "lifetime_seconds": row["lifetime_seconds"],
        "lifetime_duration": format_duration(row["lifetime_seconds"]),
        "lifetime_touches": row["lifetime_touches"],
        "max_size": row["max_size"],
        "path": path_record(row["path_bytes"]),
        "removals": row["removals"],
        "window_touches": row["window_touches"],
    }
    if row["kind"] == "directory":
        public["inferred_from"] = "tracked_paths"
        public["peak_descendants"] = row["peak_descendants"]
    return public


def _extinct_episodes(analysis: Analysis) -> list[dict[str, Any]]:
    historical = _all_paths(analysis)
    target_paths = set(analysis.target_map())
    rows = []
    for path, commit_ids in historical.items():
        if path in target_paths:
            continue
        record = _extinct_file(analysis, path, commit_ids)
        if record["absence_reason"] != "observed_deletion":
            continue
        for removal in record["removals"]:
            if removal["reason"] != "observed_deletion":
                continue
            latest = analysis.activity_span(list(analysis.repo.commits.values()))[1]
            removal_commit = analysis.repo.commits[removal["commit"]]
            end_ts = None if latest is None else latest.committer_ts
            seconds = elapsed_seconds(removal_commit.committer_ts, end_ts)
            if seconds is not None and seconds >= 365 * _DAY:
                rows.append(
                    {
                        "duration": format_duration(seconds),
                        "duration_seconds": seconds,
                        "path": path_record(path),
                        "removed_at": removal["removed_at"],
                    }
                )
    rows.sort(key=lambda row: (-row["duration_seconds"], row["path"]["path_bytes_base64"]))
    return rows


def _history(analysis: Analysis) -> tuple[dict[str, Any], dict[str, Any]]:
    path = analysis.request.path or b""
    _reject_bad_path(path)
    historical = _all_paths(analysis)
    target_paths = set(analysis.target_map())
    if path not in historical and path not in target_paths:
        if any(item.startswith(path + b"/") for item in set(historical) | target_paths):
            raise ToolError(
                "USAGE",
                "That path is a directory. Use files, churn, or extinct for directory summaries.",
                2,
                {"path": path.decode("utf-8", "surrogateescape")},
            )
        raise ToolError(
            "PATH_NOT_FOUND",
            "That path was not found in the selected history.",
            4,
            {"path": path.decode("utf-8", "surrogateescape")},
        )
    introductions = _incarnations(analysis, path)
    chosen = _select_incarnation(analysis, path, introductions)
    traced = chosen
    events = _history_events(analysis, traced)
    selected_ids = {item.oid for item in analysis.selected()}
    window = [event for event in events if event["commit"] in selected_ids]
    largest = _largest_text(events)
    authors = sorted(
        {
            (
                analysis.repo.commits[event["commit"]].author_name,
                analysis.repo.commits[event["commit"]].author_email,
            )
            for event in events
        }
    )
    canonical = sorted(
        {analysis.canonical_author(analysis.repo.commits[event["commit"]]) for event in events}
    )
    limit = analysis.limit_of(20)
    shown = events if limit == 0 else events[:limit]
    data = {
        "canonical_contributors": [{"email": email, "name": name} for name, email in canonical],
        "confirmed_birth": _commit_ref(
            analysis.repo.commits[traced.origins[0].commit]
            if traced.status == "confirmed" and traced.origins
            else None
        ),
        "empty_selection": _empty_selection(analysis.request, analysis.selected()),
        "events": shown,
        "events_returned": len(shown),
        "events_total": len(events),
        "events_truncated": len(events) > len(shown),
        "exists_in_target": path in target_paths,
        "identity_keys": traced.keys,
        "largest_recorded_text_change": largest,
        "origin_status": traced.status,
        "path": path_record(path),
        "raw_contributors": [{"email": email, "name": name} for name, email in authors],
        "status": "complete",
        "window_events": len(window),
    }
    return data, {"history": {"status": "complete"}}


def _reject_bad_path(path: bytes) -> None:
    if not path or path.startswith(b"/") or b".." in path.split(b"/"):
        raise ToolError("USAGE", "Pass one repository-relative path without '..' components.", 2)


def _incarnations(analysis: Analysis, path: bytes) -> list[_Trace]:
    traces = []
    seen: set[str] = set()
    for commit in analysis.repo.commits.values():
        if path not in analysis.path_map(commit.oid):
            continue
        traced = analysis.trace(commit.oid, path)
        key = "|".join(traced.keys) or commit.oid
        if key in seen:
            continue
        seen.add(key)
        traces.append(traced)
    return traces


def _select_incarnation(analysis: Analysis, path: bytes, traces: list[_Trace]) -> _Trace:
    if analysis.request.incarnation:
        oid = analysis.repo.peel_commit(analysis.request.incarnation)
        if oid not in analysis.repo.commits or path not in analysis.path_map(oid):
            raise ToolError(
                "AMBIGUOUS_INCARNATION",
                "The incarnation commit is not reachable or does not contain that path.",
                4,
                {"commit": analysis.request.incarnation},
            )
        return analysis.trace(oid, path)
    if path in analysis.target_map():
        return analysis.trace(analysis.target, path)
    if len(traces) == 1:
        return traces[0]
    if len(traces) == 0:
        raise ToolError("PATH_NOT_FOUND", "That path was not found in the selected history.", 4)
    listed = sorted({origin.commit for trace in traces for origin in trace.origins})
    raise ToolError(
        "AMBIGUOUS_INCARNATION",
        "Several incarnations contain that path. Pass --incarnation with one observed commit: "
        + ", ".join(listed),
        4,
        {"commits": listed},
    )


def _history_events(analysis: Analysis, traced: _Trace) -> list[dict[str, Any]]:
    order = _topo(analysis)
    rank = {oid: index for index, oid in enumerate(order)}
    events = analysis.touches(traced)
    events.sort(key=lambda item: (rank.get(item.commit, 0), item.commit))
    rows = []
    for event in events:
        commit = analysis.repo.commits[event.commit]
        rows.append(
            {
                "additions": event.additions,
                "author_email": commit.author_email,
                "author_name": commit.author_name,
                "binary": event.binary,
                "commit": event.commit,
                "committer_at": from_unix(commit.committer_ts),
                "deletions": event.deletions,
                "new_mode": event.new_mode,
                "new_object": event.new_oid,
                "new_path": None if event.new_path is None else path_record(event.new_path),
                "old_mode": event.old_mode,
                "old_object": event.old_oid,
                "old_path": None if event.old_path is None else path_record(event.old_path),
                "similarity": event.similarity,
                "status": event.status,
            }
        )
    return rows


def _largest_text(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    measured = [
        event
        for event in events
        if not event["binary"] and event["additions"] is not None and event["deletions"] is not None
    ]
    if not measured:
        return None

    def key(event: dict[str, Any]) -> tuple[Any, ...]:
        churn = (event["additions"] or 0) + (event["deletions"] or 0)
        return (-churn, event["committer_at"], event["commit"])

    winner = min(measured, key=key)
    return {
        "additions": winner["additions"],
        "commit": winner["commit"],
        "committer_at": winner["committer_at"],
        "deletions": winner["deletions"],
        "label": "largest recorded text change",
    }


def _topo(analysis: Analysis) -> list[str]:
    children: dict[str, list[str]] = defaultdict(list)
    waiting = {}
    for oid, commit in analysis.repo.commits.items():
        parents = [parent for parent in commit.parents if parent in analysis.repo.commits]
        waiting[oid] = len(parents)
        for parent in parents:
            children[parent].append(oid)
    available = sorted(oid for oid, count in waiting.items() if count == 0)
    ordered: list[str] = []
    while available:
        oid = available.pop(0)
        ordered.append(oid)
        for child in children[oid]:
            waiting[child] -= 1
            if waiting[child] == 0:
                available.append(child)
        available.sort()
    return ordered


def _churn(analysis: Analysis) -> tuple[dict[str, Any], dict[str, Any]]:
    if analysis.request.by == "directories":
        return _churn_directories(analysis)
    grouped: dict[str, dict[str, Any]] = {}
    survivor_keys = {
        _identity_key(analysis.trace(analysis.target, path)): path for path in analysis.target_map()
    }
    selected_ids = {item.oid for item in analysis.selected()}
    windowed = _activity_filtered(analysis.request)
    scope = analysis.scope_path()
    for event in analysis.repo.changes:
        if windowed and event.commit not in selected_ids:
            continue
        if not _event_in_scope(event, scope):
            continue
        if event.status == "D" and event.old_path is not None:
            parent = next(
                (
                    item
                    for item in analysis.repo.commits[event.commit].parents
                    if item in analysis.repo.commits
                ),
                None,
            )
            if parent is None or event.old_path not in analysis.path_map(parent):
                continue
            traced = analysis.trace(parent, event.old_path)
            display = event.old_path
        else:
            current = event.new_path or event.old_path or b""
            traced = analysis.trace(event.commit, current)
            display = current
        key = _identity_key(traced)
        if key in survivor_keys:
            display = survivor_keys[key]
        bucket = grouped.setdefault(
            key,
            {
                "additions": 0,
                "binary_events": 0,
                "commits": set(),
                "contributors": set(),
                "deletions": 0,
                "identity_key": key,
                "path_bytes": display,
                "text_events": 0,
            },
        )
        bucket["commits"].add(event.commit)
        bucket["contributors"].add(analysis.canonical_author(analysis.repo.commits[event.commit]))
        if event.binary:
            bucket["binary_events"] += 1
        else:
            bucket["text_events"] += 1
            bucket["additions"] += event.additions or 0
            bucket["deletions"] += event.deletions or 0
    rows = []
    for bucket in grouped.values():
        complete = bucket["binary_events"] == 0 or bucket["text_events"] > 0
        if bucket["text_events"] == 0 and bucket["binary_events"] > 0:
            complete = False
        rows.append(
            {
                "additions": bucket["additions"],
                "binary_events": bucket["binary_events"],
                "churn": bucket["additions"] + bucket["deletions"],
                "commits": len(bucket["commits"]),
                "contributors": len(bucket["contributors"]),
                "deletions": bucket["deletions"],
                "identity_key": bucket["identity_key"],
                "path": path_record(bucket["path_bytes"]),
                "path_bytes": bucket["path_bytes"],
                "text_churn_complete": complete
                if bucket["text_events"] or bucket["binary_events"]
                else True,
            }
        )
    mode = analysis.request.sort or "churn"
    if mode == "commits":
        rows.sort(key=lambda row: (-row["commits"], row["path_bytes"]))
    elif mode == "contributors":
        rows.sort(key=lambda row: (-row["contributors"], row["path_bytes"]))
    elif mode == "path":
        rows.sort(key=lambda row: (row["path_bytes"], row["identity_key"]))
    else:
        rows.sort(key=lambda row: (-row["churn"], row["path_bytes"], row["identity_key"]))
    for row in rows:
        del row["path_bytes"]
    payload = analysis.slice_rows(rows, 20)
    payload["by"] = "files"
    payload["empty_selection"] = _empty_selection(analysis.request, analysis.selected())
    return _complete(payload), {"churn": {"status": "complete"}}


def _churn_directories(analysis: Analysis) -> tuple[dict[str, Any], dict[str, Any]]:
    depth = analysis.request.depth
    grouped: dict[bytes, dict[str, Any]] = {}

    def bucket_for(path: bytes) -> dict[str, Any]:
        return grouped.setdefault(
            _directory_group(path, depth),
            {
                "additions": 0,
                "binary_events": 0,
                "commits": set(),
                "contributors": set(),
                "deletions": 0,
                "text_events": 0,
            },
        )

    selected_ids = {item.oid for item in analysis.selected()}
    scope = analysis.scope_path()
    for event in analysis.repo.changes:
        if event.commit not in selected_ids and _activity_filtered(analysis.request):
            continue
        if not _event_in_scope(event, scope):
            continue
        author = analysis.canonical_author(analysis.repo.commits[event.commit])
        if event.status == "R" and event.old_path is not None and event.new_path is not None:
            old_group = _directory_group(event.old_path, depth)
            new_group = _directory_group(event.new_path, depth)
            if old_group == new_group:
                bucket = bucket_for(event.new_path)
                bucket["commits"].add(event.commit)
                bucket["contributors"].add(author)
                _dir_lines(bucket, event, event.additions, event.deletions)
            else:
                old_bucket = grouped.setdefault(old_group, bucket_for(event.old_path))
                new_bucket = grouped.setdefault(new_group, bucket_for(event.new_path))
                old_bucket["commits"].add(event.commit)
                new_bucket["commits"].add(event.commit)
                old_bucket["contributors"].add(author)
                new_bucket["contributors"].add(author)
                _dir_lines(old_bucket, event, 0, 0 if event.binary else event.deletions)
                _dir_lines(new_bucket, event, 0 if event.binary else event.additions, 0)
        else:
            path = event.new_path or event.old_path or b"."
            bucket = bucket_for(path)
            bucket["commits"].add(event.commit)
            bucket["contributors"].add(author)
            _dir_lines(bucket, event, event.additions, event.deletions)
    rows = []
    for path, bucket in grouped.items():
        rows.append(
            {
                "additions": bucket["additions"],
                "binary_events": bucket["binary_events"],
                "churn": bucket["additions"] + bucket["deletions"],
                "commits": len(bucket["commits"]),
                "contributors": len(bucket["contributors"]),
                "deletions": bucket["deletions"],
                "path": path_record(path),
                "path_bytes": path,
                "text_churn_complete": not (
                    bucket["text_events"] == 0 and bucket["binary_events"] > 0
                ),
            }
        )
    if scope is not None:
        rows = [row for row in rows if _path_in_scope(row["path_bytes"], scope)]
    rows.sort(key=lambda row: (-row["churn"], row["path_bytes"]))
    for row in rows:
        del row["path_bytes"]
    payload = analysis.slice_rows(rows, 20)
    payload["by"] = "directories"
    payload["depth"] = depth
    return _complete(payload), {"churn": {"status": "complete"}}


def _directory_group(path: bytes, depth: int) -> bytes:
    parts = path.split(b"/")
    if len(parts) == 1:
        return b"."
    return b"/".join(parts[:-1][:depth])


def _dir_lines(
    bucket: dict[str, Any], event: Change, additions: int | None, deletions: int | None
) -> None:
    if event.binary:
        bucket["binary_events"] += 1
        return
    bucket["text_events"] += 1
    bucket["additions"] += additions or 0
    bucket["deletions"] += deletions or 0


def _blobs(analysis: Analysis) -> tuple[dict[str, Any], dict[str, Any]]:
    selected_ids = {item.oid for item in analysis.selected()}
    use_window = _activity_filtered(analysis.request)
    scope = analysis.scope_path()
    paths_for: dict[str, set[bytes]] = defaultdict(set)
    commits = analysis.repo.commits.values()
    chosen = [item for item in commits if item.oid in selected_ids] if use_window else list(commits)
    for commit in chosen:
        for leaf in analysis.path_map(commit.oid).values():
            if leaf.kind == "gitlink" or not _path_in_scope(leaf.path, scope):
                continue
            paths_for[leaf.oid].add(leaf.path)
    target_blob_paths: dict[str, set[bytes]] = defaultdict(set)
    target_paths = {path for path in analysis.target_map() if _path_in_scope(path, scope)}
    for path, leaf in analysis.target_map().items():
        if leaf.kind != "gitlink" and path in target_paths:
            target_blob_paths[leaf.oid].add(path)
    sizes = analysis.repo.blob_sizes(paths_for)
    rows = []
    for oid, blob_paths in paths_for.items():
        size = sizes.get(oid)
        if size is None or size < analysis.request.min_size:
            continue
        present = bool(target_blob_paths.get(oid))
        if present:
            presence = "present_in_target"
        elif blob_paths & target_paths:
            presence = "historical_version"
        else:
            presence = "historical_only"
        if analysis.request.deleted and presence == "present_in_target":
            continue
        ordered_paths = sorted(blob_paths)
        rows.append(
            {
                "id": oid,
                "lfs_classification": "not_inspected",
                "path_count": len(ordered_paths),
                "paths": [path_record(path) for path in ordered_paths],
                "presence": presence,
                "present_in_target": present,
                "representative_path": path_record(ordered_paths[0]),
                "size": size,
                "target_paths": [
                    path_record(path) for path in sorted(target_blob_paths.get(oid, ()))
                ],
            }
        )
    mode = analysis.request.sort or "size"
    if mode == "path":
        rows.sort(key=lambda row: (rows_path(row), row["id"]))
    else:
        rows.sort(key=lambda row: (-_blob_size(row), str(row["id"])))
    total_bytes = sum(_blob_size(row) for row in rows)
    payload = analysis.slice_rows(rows, 20)
    payload["min_size"] = analysis.request.min_size
    payload["total_bytes"] = total_bytes
    payload["unique_blobs"] = payload["total"]
    return _complete(payload), {"blobs": {"status": "complete"}}


def _blob_size(row: dict[str, Any]) -> int:
    size = row["size"]
    return size if isinstance(size, int) else 0


def rows_path(row: dict[str, Any]) -> str:
    return row["representative_path"]["path_bytes_base64"]


def _evolution(analysis: Analysis) -> tuple[dict[str, Any], dict[str, Any]]:
    chain = _first_parent_chain(analysis)
    in_window = []
    for index, oid in enumerate(chain):
        commit = analysis.repo.commits[oid]
        if analysis.request.since is not None and commit.committer_ts < analysis.request.since:
            continue
        if analysis.request.until is not None and commit.committer_ts >= analysis.request.until:
            continue
        in_window.append((index, commit))
    group = analysis.request.group
    if group == "auto":
        if len(in_window) < 2:
            group = "month"
        else:
            span = in_window[-1][1].committer_ts - in_window[0][1].committer_ts
            group = auto_group(span)
    samples: list[dict[str, Any]] = []
    if in_window:
        keys = bucket_range(in_window[0][1].committer_ts, in_window[-1][1].committer_ts, group)
        if len(keys) > analysis.request.max_snapshots:
            raise ToolError(
                "USAGE",
                "That grouping produces more snapshots than --max-snapshots. "
                "Choose a coarser grouping or raise the bound.",
                2,
                {"snapshots": len(keys), "max_snapshots": analysis.request.max_snapshots},
            )
        for key in keys:
            end = exclusive_bucket_end(key, group)
            candidates = [
                (analysis.repo.commits[oid].committer_ts, index, oid)
                for index, oid in enumerate(chain)
                if analysis.repo.commits[oid].committer_ts < end
                and (
                    analysis.request.until is None
                    or analysis.repo.commits[oid].committer_ts < analysis.request.until
                )
            ]
            if not candidates:
                samples.append({"bucket": key, "commit": None, "row_kind": "no_snapshot"})
                continue
            _ts, _index, oid = max(candidates)
            samples.append(_snapshot_row(analysis, key, oid, "sample"))
    target_row = _snapshot_row(analysis, "target", analysis.target, "target")
    if analysis.request.lines:
        _apply_lines(analysis, [row for row in samples if row.get("commit")] + [target_row])
    else:
        for row in samples:
            if row.get("commit"):
                row["text_lines"] = None
                row["text_lines_status"] = "not_requested"
        target_row["text_lines"] = None
        target_row["text_lines_status"] = "not_requested"
    return (
        {
            "empty_selection": _empty_selection(analysis.request, analysis.selected()),
            "group": group,
            "samples": samples,
            "snapshot_policy": "first_parent",
            "status": "complete",
            "target": target_row,
        },
        {"evolution": {"status": "complete"}},
    )


def _first_parent_chain(analysis: Analysis) -> list[str]:
    chain = []
    seen = set()
    oid: str | None = analysis.target
    while oid and oid not in seen and oid in analysis.repo.commits:
        seen.add(oid)
        chain.append(oid)
        parents = analysis.repo.commits[oid].parents
        oid = parents[0] if parents else None
    chain.reverse()
    return chain


def _snapshot_row(analysis: Analysis, bucket: str, oid: str, kind: str) -> dict[str, Any]:
    commit = analysis.repo.commits[oid]
    scope = analysis.scope_path()
    leaves = [leaf for leaf in analysis.path_map(oid).values() if _path_in_scope(leaf.path, scope)]
    sizes = analysis.repo.blob_sizes(leaf.oid for leaf in leaves if leaf.kind != "gitlink")
    regular = [leaf for leaf in leaves if leaf.kind == "file"]
    logical = 0
    extensions: dict[str, dict[str, int]] = {}
    languages: dict[str, dict[str, int]] = {}
    for leaf in leaves:
        if leaf.kind == "gitlink":
            continue
        size = sizes.get(leaf.oid, 0)
        logical += size
        if leaf.kind != "file":
            continue
        suffix = extension_of(leaf.path)
        language = language_of(leaf.path)
        for table, key in ((extensions, suffix), (languages, language)):
            slot = table.setdefault(key, {"bytes": 0, "count": 0})
            slot["count"] += 1
            slot["bytes"] += size
    regular_count = len(regular)
    regular_bytes = sum(sizes.get(leaf.oid, 0) for leaf in regular)
    prefixes = set()
    for leaf in leaves:
        parts = leaf.path.split(b"/")
        for index in range(1, len(parts)):
            prefixes.add(b"/".join(parts[:index]))
    return {
        "authors": len(_author_closure(analysis, oid)),
        "bucket": bucket,
        "commit": oid,
        "committer_at": from_unix(commit.committer_ts),
        "directories": len(prefixes),
        "extensions": _share(extensions, regular_count, regular_bytes),
        "gitlinks": sum(1 for leaf in leaves if leaf.kind == "gitlink"),
        "languages": _share(languages, regular_count, regular_bytes),
        "logical_bytes": logical,
        "regular_files": regular_count,
        "row_kind": kind,
        "symlinks": sum(1 for leaf in leaves if leaf.kind == "symlink"),
        "tracked_leaves": len(leaves),
    }


def _share(table: dict[str, dict[str, int]], count_den: int, byte_den: int) -> list[dict[str, Any]]:
    rows = []
    for key in sorted(table):
        slot = table[key]
        rows.append(
            {
                "bytes": slot["bytes"],
                "byte_percentage": percentage(slot["bytes"], byte_den),
                "count": slot["count"],
                "count_percentage": percentage(slot["count"], count_den),
                "denominator": "regular_files",
                "name": key,
            }
        )
    return rows


def _author_closure(analysis: Analysis, oid: str) -> frozenset[tuple[str, str]]:
    cached = analysis._authors.get(oid)
    if cached is not None:
        return cached
    for current in _topo(analysis):
        if current in analysis._authors:
            continue
        commit = analysis.repo.commits[current]
        found = {analysis.canonical_author(commit)}
        for parent in commit.parents:
            parent_authors = analysis._authors.get(parent)
            if parent_authors is not None:
                found |= parent_authors
        analysis._authors[current] = frozenset(found)
    return analysis._authors[oid]


def _apply_lines(analysis: Analysis, rows: list[dict[str, Any]]) -> None:
    needed: set[str] = set()
    per_row: list[tuple[dict[str, Any], list[Leaf]]] = []
    for row in rows:
        leaves = [leaf for leaf in analysis.path_map(row["commit"]).values() if leaf.kind == "file"]
        per_row.append((row, leaves))
        needed.update(leaf.oid for leaf in leaves)
    bodies = analysis.repo.blob_bodies(needed)
    measured: dict[str, tuple[int | None, str, int]] = {}
    for oid, body in bodies.items():
        measured[oid] = _classify_body(body)
    for row, leaves in per_row:
        lines = 0
        text_bytes = 0
        excluded = {"binary": 0, "lfs": 0}
        for leaf in leaves:
            info = measured.get(leaf.oid)
            if info is None:
                continue
            text_lines, label, declared = info
            if label == "binary":
                excluded["binary"] += 1
            elif label == "lfs":
                excluded["lfs"] += 1
                row.setdefault("lfs_declared_bytes", 0)
                row["lfs_declared_bytes"] = row.get("lfs_declared_bytes", 0) + declared
            elif text_lines is not None:
                lines += text_lines
                text_bytes += len(bodies[leaf.oid])
        row["excluded"] = excluded
        row["text_bytes_measured"] = text_bytes
        row["text_lines"] = lines
        row["text_lines_status"] = "complete"


def _classify_body(body: bytes) -> tuple[int | None, str, int]:
    if b"\0" in body[:8000]:
        return None, "binary", 0
    declared = _lfs_size(body)
    if declared is not None:
        return None, "lfs", declared
    if not body:
        return 0, "text", 0
    lines = body.count(b"\n")
    if not body.endswith(b"\n"):
        lines += 1
    return lines, "text", 0


def _lfs_size(body: bytes) -> int | None:
    if len(body) > 1024 or not body.startswith(_LFS_VERSION):
        return None
    try:
        text = body.decode("ascii")
    except UnicodeDecodeError:
        return None
    oid_ok = False
    size: int | None = None
    for line in text.splitlines():
        if line.startswith("oid sha256:"):
            value = line.split(":", 1)[1]
            oid_ok = len(value) == 64 and all(char in "0123456789abcdef" for char in value)
        elif line.startswith("size ") and line[5:].isdigit():
            size = int(line[5:])
    if oid_ok and size is not None:
        return size
    return None
