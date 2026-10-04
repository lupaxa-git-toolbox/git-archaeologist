"""Console, JSON, and Markdown renderers. They do not recompute metrics."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from colored import Fore, Style

from lupaxa.git_archaeologist.console import console_lines
from lupaxa.git_archaeologist.options import Request
from lupaxa.git_archaeologist.text import escape_cell, format_iec


def render(report: dict[str, Any], request: Request) -> None:
    """Write one report to stdout."""
    if request.fmt == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        sys.stdout.write("\n")
        return
    if request.fmt == "markdown":
        sys.stdout.write("\n".join(_markdown(report)) + "\n")
        return
    sys.stdout.write("\n".join(_console(report, request)) + "\n")


def render_error(report: dict[str, Any], fmt: str) -> None:
    """Write a failure. JSON stays on stdout. Other formats use stderr."""
    if fmt == "json":
        sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        sys.stdout.write("\n")
        return
    errors = report.get("errors") or []
    message = errors[0]["message"] if errors else "Git Archaeologist failed."
    label = "Error"
    if fmt == "console" and sys.stderr.isatty() and "NO_COLOR" not in os.environ:
        label = f"{Style.bold}{Fore.red}{label}{Style.reset}"
    sys.stderr.write(f"{label}: {message}\n")


def _console(report: dict[str, Any], request: Request) -> list[str]:
    return console_lines(report, request)


def _row_summary(row: dict[str, Any]) -> str:
    path = row.get("path")
    label = path["path"] if isinstance(path, dict) else row.get("bucket") or row.get("id") or ""
    if "commits" in row and "canonical_name" in row:
        return f"{row['canonical_name']} <{row['canonical_email']}>  commits={row['commits']}"
    if "churn" in row and "commits" in row:
        return f"{label}  churn={row['churn']}  commits={row['commits']}"
    if "size" in row and "presence" in row:
        return f"{format_iec(row['size'])}  {row['presence']}  {row['id']}"
    if "window_touches" in row:
        return f"{label}  touches={row['window_touches']}  origin={row.get('origin_status')}"
    if "lifetime_touches" in row:
        return f"{label}  touches={row['lifetime_touches']}  origin={row.get('origin_status')}"
    return str(label)


def _markdown(report: dict[str, Any]) -> list[str]:
    lines = [f"# Git Archaeologist {report['tool_version']}", ""]
    repository = report.get("repository") or {}
    scope = report.get("scope") or {}
    analysis = report.get("analysis") or {}
    lines.append("## Scope")
    lines.append("")
    if repository:
        lines.append(f"- Repository: `{repository.get('display_name')}`")
        lines.append(f"- State: `{repository.get('repository_state')}`")
        lines.append(f"- Bare: `{repository.get('bare')}`")
        lines.append(f"- Shallow: `{repository.get('shallow')}`")
    if scope:
        lines.append(f"- Selector: `{scope.get('selector_kind')}` `{scope.get('selector_value')}`")
        lines.append(f"- Target: `{scope.get('target_id')}`")
        lines.append(
            f"- Commits: {scope.get('selected_commits')} selected, "
            f"{scope.get('reachable_commits')} reachable"
        )
        if scope.get("since") or scope.get("until"):
            lines.append(f"- Interval: `{scope.get('since')}` to `{scope.get('until')}`")
    if analysis.get("merge_policy_label"):
        lines.append(f"- Policy: {analysis['merge_policy_label']}")
    if analysis.get("history_complete") is False:
        lines.append("- History is incomplete because the repository is shallow.")
    lines.append("")
    data = report.get("data") or {}
    if data.get("message"):
        lines.append(str(data["message"]))
        lines.append("")
    if data.get("empty_selection"):
        lines.append(str(data["empty_selection"]))
        lines.append("")
    lines.extend(_markdown_sections(report["command"], data))
    warnings = report.get("warnings") or []
    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for warning in warnings:
            lines.append(f"- `{warning['code']}`: {warning['message']}")
        lines.append("")
    skipped = [
        (name, section.get("reason"))
        for name, section in (analysis.get("sections") or {}).items()
        if section.get("status") == "skipped"
    ]
    if skipped:
        lines.append("## Skipped Sections")
        lines.append("")
        for name, reason in skipped:
            lines.append(f"- {name}: {reason}")
        lines.append("")
    return lines


def _markdown_sections(command: str, data: dict[str, Any]) -> list[str]:
    if command == "dig":
        return _markdown_dig(data)
    if command == "timeline":
        return _markdown_table(
            "Timeline",
            ["Bucket", "Commits", "Authors", "Churn"],
            [
                [bucket["key"], bucket["commits"], bucket["authors"], bucket["churn"]]
                for bucket in data.get("buckets") or []
            ],
        )
    if command == "evolution":
        snapshot_rows: list[list[Any]] = []
        for row in data.get("samples") or []:
            if row.get("row_kind") == "no_snapshot":
                snapshot_rows.append([row["bucket"], "no snapshot", "", ""])
            else:
                snapshot_rows.append(
                    [row["bucket"], row["commit"], row["tracked_leaves"], row["logical_bytes"]]
                )
        return _markdown_table("Snapshots", ["Bucket", "Commit", "Leaves", "Bytes"], snapshot_rows)
    if command == "history":
        lines = ["## History", "", f"Origin status: `{data.get('origin_status')}`", ""]
        events = data.get("events") or []
        lines.extend(
            _markdown_table(
                "Events",
                ["Commit", "Status", "When"],
                [[event["commit"], event["status"], event["committer_at"]] for event in events],
            )
        )
        return lines
    rows = [row for row in data.get("rows") or [] if isinstance(row, dict)]
    return _markdown_table("Results", ["Summary"], [[_cell(_row_summary(row))] for row in rows])


def _markdown_dig(data: dict[str, Any]) -> list[str]:
    lines = ["## Overview", ""]
    summary = data.get("repository_summary") or {}
    lines.append(f"- Target entries: {summary.get('target_entries')}")
    lines.append(f"- Canonical authors: {summary.get('canonical_author_count')}")
    lines.append("")
    for name in ("survivors", "most_changed", "extinct", "blobs"):
        section = data.get(name) or {}
        lines.append(f"## {name}")
        lines.append("")
        if section.get("status") == "skipped":
            lines.append(f"Skipped: {section.get('reason')}")
            lines.append("")
            continue
        rows = section.get("rows") or section.get("sample") or []
        lines.append(f"{len(rows)} row(s) shown.")
        lines.append("")
    return lines


def _markdown_table(title: str, headers: list[str], rows: list[list[Any]]) -> list[str]:
    lines = [f"## {title}", ""]
    if not rows:
        lines.append("No rows.")
        lines.append("")
        return lines
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")
    for row in rows:
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    lines.append("")
    return lines


def _cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("`", "\\`").replace("<", "\\<").replace(">", "\\>")


def path_cell(record: dict[str, str]) -> str:
    """Escape a path record for a table. Exposed for tests."""
    import base64

    return escape_cell(base64.b64decode(record["path_bytes_base64"]))
