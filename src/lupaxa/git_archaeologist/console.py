"""Coloured, aligned console reports. JSON and Markdown do not use this module."""

from __future__ import annotations

import re
from typing import Any

from colored import Fore, Style

from lupaxa.git_archaeologist.options import Request
from lupaxa.git_archaeologist.text import abbreviate, format_iec

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Paint:
    """Apply ANSI styles only when the caller asked for colour."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, text: str, *styles: str) -> str:
        if not self.enabled or not styles or text == "":
            return text
        return f"{''.join(styles)}{text}{Style.reset}"


def console_lines(report: dict[str, Any], request: Request) -> list[str]:
    """Render one console report. Colour is already decided by the caller."""
    paint = Paint(request.color)
    width = max(request.width, 40)
    lines = _header(report, paint)
    data = report.get("data") or {}
    note = data.get("message") or data.get("empty_selection")
    if note:
        lines.append(paint(str(note), Style.dim))
    lines.extend(_command(report.get("command") or "", data, paint, width, request))
    warnings = report.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append(paint("Warnings", Style.bold, Fore.yellow))
        for warning in warnings:
            lines.append(
                "  "
                + paint(str(warning.get("code")), Fore.yellow)
                + "  "
                + str(warning.get("message"))
            )
    return lines


def render_table(
    headers: list[str],
    rows: list[list[str]],
    *,
    aligns: list[str] | None = None,
    width: int = 80,
    paint: Paint | None = None,
    colours: list[str] | None = None,
) -> list[str]:
    """Draw a box table. ``colours`` styles the whole column when colour is on."""
    painter = paint or Paint(False)
    if not rows:
        return [painter("No rows.", Style.dim)]
    column_aligns = aligns or ["left"] * len(headers)
    column_colours = colours or [""] * len(headers)
    widths = _fit_widths(headers, rows, width)
    shown = [_clip_row(row, widths) for row in rows]
    rule_top = _rule("╭", "┬", "╮", widths)
    rule_mid = _rule("├", "┼", "┤", widths)
    rule_bot = _rule("╰", "┴", "╯", widths)
    lines = [
        painter(rule_top, Style.dim),
        _data_row(headers, widths, column_aligns, painter, [Style.bold] * len(headers)),
        painter(rule_mid, Style.dim),
    ]
    for row in shown:
        lines.append(_data_row(row, widths, column_aligns, painter, column_colours))
    lines.append(painter(rule_bot, Style.dim))
    return lines


def _header(report: dict[str, Any], paint: Paint) -> list[str]:
    repository = report.get("repository") or {}
    scope = report.get("scope") or {}
    analysis = report.get("analysis") or {}
    lines = [
        paint(f"Git Archaeologist {report.get('tool_version')}", Style.bold, Fore.cyan),
    ]
    if repository:
        lines.append(_field("Repository", str(repository.get("display_name") or ""), paint))
        lines.append(
            _field(
                "State",
                _state(str(repository.get("repository_state") or ""), paint),
                paint,
                raw=True,
            )
        )
    if scope.get("target_id"):
        target = str(scope["target_id"])
        lines.append(_field("Target", abbreviate(target, {target}), paint, Fore.white))
        lines.append(
            _field(
                "Commits",
                f"{_commas(scope.get('selected_commits') or 0)} selected, "
                f"{_commas(scope.get('reachable_commits') or 0)} reachable",
                paint,
            )
        )
    if scope.get("since") or scope.get("until"):
        lines.append(
            _field(
                "Interval",
                f"{scope.get('since') or 'open'} .. {scope.get('until') or 'open'}",
                paint,
            )
        )
    if analysis.get("merge_policy_label"):
        lines.append(paint(str(analysis["merge_policy_label"]), Style.dim))
    return lines


def _field(label: str, value: str, paint: Paint, colour: str = "", *, raw: bool = False) -> str:
    shown = value if raw or not colour else paint(value, colour)
    return f"{paint(f'{label:<10}', Style.dim)}  {shown}"


def _state(state: str, paint: Paint) -> str:
    colour = {
        "normal": Fore.green,
        "empty": Style.dim,
        "unborn_head": Fore.yellow,
        "shallow": Fore.yellow,
    }.get(state, "")
    return paint(state, colour) if colour else state


def _command(
    command: str, data: dict[str, Any], paint: Paint, width: int, request: Request
) -> list[str]:
    if command == "dig":
        return _dig(data, paint, width)
    if command == "timeline":
        return _timeline(data, paint, width)
    if command == "evolution":
        return _evolution(data, paint, width)
    if command == "history":
        return _history(data, paint, width, details=request.events)
    rows = data.get("rows")
    if not isinstance(rows, list):
        return []
    return _section(
        command,
        _count_note(data),
        _rows_table(command, rows, paint, width),
        paint,
    )


def _dig(data: dict[str, Any], paint: Paint, width: int) -> list[str]:
    summary = data.get("repository_summary") or {}
    lines = _section(
        "Overview",
        "",
        render_table(
            ["Field", "Value"],
            [
                ["Target entries", _commas(summary.get("target_entries") or 0)],
                ["Canonical authors", _commas(summary.get("canonical_author_count") or 0)],
            ],
            aligns=["left", "right"],
            width=width,
            paint=paint,
        ),
        paint,
    )
    for name, title in (
        ("survivors", "Survivors"),
        ("most_changed", "Most changed"),
        ("extinct", "Extinct"),
        ("blobs", "Historical blobs"),
    ):
        section = data.get(name) or {}
        if section.get("status") == "skipped":
            lines.extend(
                _section(
                    title,
                    paint(f"Skipped ({section.get('reason')}).", Style.dim),
                    [],
                    paint,
                )
            )
            continue
        rows = section.get("rows") or section.get("sample") or []
        kind = "extinct" if name == "extinct" else "files" if name == "most_changed" else name
        lines.extend(_section(title, "", _rows_table(kind, rows, paint, width), paint))
    discoveries = data.get("discoveries") or {}
    if discoveries:
        lines.extend(_discoveries(discoveries, paint, width))
    return lines


def _discoveries(discoveries: dict[str, Any], paint: Paint, width: int) -> list[str]:
    lines = [""]
    lines.append(paint("Discoveries", Style.bold, Fore.cyan))
    for key, title, headers, builder in (
        ("gaps", "Gaps of at least 90 days", ["Duration", "From", "To"], _gap_cells),
        (
            "extinct_episodes",
            "Extinct episodes of at least 365 days",
            ["Duration", "Path", "Removed"],
            _episode_cells,
        ),
        (
            "historical_blobs",
            "Historical-only blobs of at least 10 MiB",
            ["Size", "Presence", "Path", "Object"],
            _blob_cells,
        ),
    ):
        section = discoveries.get(key) or {}
        lines.append("")
        lines.append(paint(title, Style.bold))
        if section.get("status") == "skipped":
            lines.append(paint(f"Skipped ({section.get('reason')}).", Style.dim))
            continue
        rows = section.get("rows") or []
        lines.extend(
            render_table(headers, [builder(row) for row in rows], width=width, paint=paint)
        )
    return lines


def _timeline(data: dict[str, Any], paint: Paint, width: int) -> list[str]:
    buckets = data.get("buckets") or []
    rows = [
        [
            str(bucket.get("key")),
            _commas(bucket.get("commits") or 0),
            _commas(bucket.get("authors") or 0),
            _commas(bucket.get("added") or 0),
            _commas(bucket.get("deleted") or 0),
            _commas(bucket.get("renames") or 0),
            _commas(bucket.get("churn") or 0),
        ]
        for bucket in buckets
    ]
    return _section(
        f"Timeline ({data.get('group')})",
        "",
        render_table(
            ["Bucket", "Commits", "Authors", "Added", "Deleted", "Renames", "Churn"],
            rows,
            aligns=["left", "right", "right", "right", "right", "right", "right"],
            width=width,
            paint=paint,
        ),
        paint,
    )


def _evolution(data: dict[str, Any], paint: Paint, width: int) -> list[str]:
    peers = {str(row.get("commit")) for row in (data.get("samples") or []) if row.get("commit")}
    target = data.get("target") or {}
    if target.get("commit"):
        peers.add(str(target["commit"]))
    rows = [_snapshot_cells(row, peers) for row in data.get("samples") or []]
    if target.get("commit"):
        rows.append(_snapshot_cells(target, peers))
    return _section(
        f"Evolution ({data.get('snapshot_policy')}, {data.get('group')})",
        "",
        render_table(
            ["Bucket", "Commit", "Leaves", "Files", "Bytes", "Authors"],
            rows,
            aligns=["left", "left", "right", "right", "right", "right"],
            width=width,
            paint=paint,
        ),
        paint,
    )


def _history(data: dict[str, Any], paint: Paint, width: int, *, details: bool) -> list[str]:
    origin = str(data.get("origin_status") or "")
    colour = _origin_colour(origin)
    origin_text = paint(f"Origin {origin}", colour) if colour else f"Origin {origin}"
    lines = _section(
        "History",
        f"{origin_text}  Events {_commas(data.get('events_total') or 0)}",
        [],
        paint,
    )
    events = data.get("events") or []
    if not details or not events:
        return lines
    peers = {str(event.get("commit")) for event in events}
    rows = []
    for event in events:
        old = _path(event.get("old_path"))
        new = _path(event.get("new_path"))
        shown = new if not old or old == new else f"{old} -> {new}"
        rows.append(
            [
                abbreviate(str(event.get("commit")), peers),
                _short_time(event.get("committer_at")),
                str(event.get("status") or ""),
                shown,
                _delta(event),
            ]
        )
    lines.extend(
        render_table(
            ["Commit", "When", "Status", "Path", "+/-"],
            rows,
            aligns=["left", "left", "left", "left", "right"],
            width=width,
            paint=paint,
            colours=["", "", "status", "", ""],
        )
    )
    return lines


def _rows_table(command: str, rows: list[Any], paint: Paint, width: int) -> list[str]:
    records = [row for row in rows if isinstance(row, dict)]
    if command == "contributors":
        return render_table(
            ["Name", "Email", "Commits", "%", "First", "Last"],
            [
                [
                    str(row.get("canonical_name") or ""),
                    str(row.get("canonical_email") or ""),
                    _commas(row.get("commits") or 0),
                    _percent(row.get("percentage")),
                    _short_time((row.get("first_selected") or {}).get("committer_at")),
                    _short_time((row.get("last_selected") or {}).get("committer_at")),
                ]
                for row in records
            ],
            aligns=["left", "left", "right", "right", "left", "left"],
            width=width,
            paint=paint,
        )
    if command == "survivors":
        return render_table(
            ["Path", "Origin", "Earliest", "Touches", "Authors", "Renames"],
            [
                [
                    _path(row.get("path")),
                    str(row.get("origin_status") or ""),
                    _short_time((row.get("earliest_observed") or {}).get("committer_at")),
                    _commas(row.get("lifetime_touches") or 0),
                    _commas(row.get("lifetime_contributors") or 0),
                    _commas(row.get("inferred_renames") or 0),
                ]
                for row in records
            ],
            aligns=["left", "left", "left", "right", "right", "right"],
            width=width,
            paint=paint,
            colours=["", "origin", "", "", "", ""],
        )
    if command == "files":
        return render_table(
            ["Path", "Kind", "Size", "Touches", "Churn", "Authors", "Origin"],
            [_file_cells(row) for row in records],
            aligns=["left", "left", "right", "right", "right", "right", "left"],
            width=width,
            paint=paint,
            colours=["", "", "", "", "", "", "origin"],
        )
    if command == "extinct":
        return render_table(
            ["Path", "Kind", "Reason", "Last seen", "Touches", "Authors"],
            [
                [
                    _path(row.get("path")),
                    str(row.get("kind") or ""),
                    str(row.get("absence_reason") or ""),
                    _short_time((row.get("last_observed") or {}).get("committer_at")),
                    _commas(row.get("lifetime_touches") or 0),
                    _commas(row.get("lifetime_contributors") or 0),
                ]
                for row in records
            ],
            aligns=["left", "left", "left", "left", "right", "right"],
            width=width,
            paint=paint,
        )
    if command == "churn":
        return render_table(
            ["Path", "Churn", "Added", "Deleted", "Commits", "Authors", "Binary"],
            [
                [
                    _path(row.get("path")),
                    _commas(row.get("churn") or 0),
                    _commas(row.get("additions") or 0),
                    _commas(row.get("deletions") or 0),
                    _commas(row.get("commits") or 0),
                    _commas(row.get("contributors") or 0),
                    _commas(row.get("binary_events") or 0),
                ]
                for row in records
            ],
            aligns=["left", "right", "right", "right", "right", "right", "right"],
            width=width,
            paint=paint,
        )
    if command == "blobs":
        return render_table(
            ["Size", "Presence", "Path", "Object"],
            [_blob_cells(row) for row in records],
            aligns=["right", "left", "left", "left"],
            width=width,
            paint=paint,
            colours=["", "presence", "", ""],
        )
    return render_table(
        ["Value"],
        [[str(row)] for row in records],
        width=width,
        paint=paint,
    )


def _file_cells(row: dict[str, Any]) -> list[str]:
    current = row.get("current") or {}
    size = current.get("size") if isinstance(current, dict) else None
    touches = row.get("touches", row.get("lifetime_touches"))
    churn = row.get("churn", row.get("lifetime_churn"))
    authors = row.get("contributors", row.get("lifetime_contributors"))
    return [
        _path(row.get("path")),
        str(current.get("kind") or "") if isinstance(current, dict) else "",
        "" if size is None else format_iec(size),
        "" if touches is None else _commas(touches),
        "" if churn is None else _commas(churn),
        "" if authors is None else _commas(authors),
        str(row.get("origin_status") or ""),
    ]


def _blob_cells(row: dict[str, Any]) -> list[str]:
    peers = {str(row.get("id"))}
    path = row.get("representative_path") or {}
    extra = int(row.get("path_count") or 1) - 1
    label = _path(path)
    if extra > 0:
        label = f"{label} (+{extra})"
    return [
        format_iec(int(row.get("size") or 0)),
        str(row.get("presence") or ""),
        label,
        abbreviate(str(row.get("id")), peers),
    ]


def _gap_cells(row: dict[str, Any]) -> list[str]:
    return [
        str(row.get("duration") or ""),
        _short_time((row.get("from") or {}).get("committer_at")),
        _short_time((row.get("to") or {}).get("committer_at")),
    ]


def _episode_cells(row: dict[str, Any]) -> list[str]:
    return [
        str(row.get("duration") or ""),
        _path(row.get("path")),
        _short_time(row.get("removed_at")),
    ]


def _snapshot_cells(row: dict[str, Any], peers: set[str]) -> list[str]:
    if row.get("row_kind") == "no_snapshot" or not row.get("commit"):
        return [str(row.get("bucket") or ""), "no snapshot", "", "", "", ""]
    return [
        str(row.get("bucket") or ""),
        abbreviate(str(row["commit"]), peers),
        _commas(row.get("tracked_leaves") or 0),
        _commas(row.get("regular_files") or 0),
        format_iec(int(row.get("logical_bytes") or 0)),
        _commas(row.get("authors") or 0),
    ]


def _section(title: str, note: str, body: list[str], paint: Paint) -> list[str]:
    lines = ["", paint(title[:1].upper() + title[1:], Style.bold, Fore.cyan)]
    if note:
        lines.append(note)
    lines.extend(body)
    return lines


def _count_note(data: dict[str, Any]) -> str:
    if "returned" not in data:
        return ""
    shown = _commas(data.get("returned") or 0)
    total = _commas(data.get("total") or 0)
    suffix = "  truncated" if data.get("truncated") else ""
    return f"Showing {shown} of {total}{suffix}"


def _data_row(
    cells: list[str],
    widths: list[int],
    aligns: list[str],
    paint: Paint,
    colours: list[str],
) -> str:
    drawn = []
    for index, cell in enumerate(cells):
        colour = _resolve_colour(colours[index] if index < len(colours) else "", cell)
        text = paint(cell, colour) if colour else cell
        gap = widths[index] - _visible(cell)
        shown = (" " * gap) + text if aligns[index] == "right" else text + (" " * gap)
        drawn.append(shown)
    return "│ " + " │ ".join(drawn) + " │"


def _resolve_colour(token: str, cell: str) -> str:
    if token in {
        Style.bold,
        Fore.green,
        Fore.yellow,
        Fore.red,
        Fore.cyan,
        Fore.magenta,
        Fore.white,
    }:
        return token
    if token == "status":
        return _status_colour(cell)
    if token == "origin":
        return _origin_colour(cell)
    if token == "presence":
        return {
            "present_in_target": Fore.green,
            "historical_version": Fore.yellow,
            "historical_only": Style.dim,
        }.get(cell, "")
    if token:
        return token
    return ""


def _origin_colour(status: str) -> str:
    return {"confirmed": Fore.green, "boundary": Fore.yellow, "ambiguous": Fore.yellow}.get(
        status, ""
    )


def _status_colour(status: str) -> str:
    return {
        "A": Fore.green,
        "M": Fore.cyan,
        "D": Fore.yellow,
        "R": Fore.magenta,
        "C": Fore.green,
        "T": Fore.white,
    }.get(status, "")


def _fit_widths(headers: list[str], rows: list[list[str]], max_width: int) -> list[int]:
    count = len(headers)
    widths = [len(header) for header in headers]
    for row in rows:
        for index in range(count):
            widths[index] = max(widths[index], len(row[index]) if index < len(row) else 0)
    chrome = 3 * count + 1
    floor = 8
    while sum(widths) + chrome > max_width:
        wide = [index for index, size in enumerate(widths) if size > floor]
        if not wide:
            break
        chosen = max(wide, key=lambda index: (widths[index], index))
        widths[chosen] -= 1
    return widths


def _clip_row(row: list[str], widths: list[int]) -> list[str]:
    clipped = []
    for index, width in enumerate(widths):
        cell = row[index] if index < len(row) else ""
        if len(cell) > width:
            cell = cell[: max(width - 3, 1)] + "..."
        clipped.append(cell)
    return clipped


def _rule(left: str, mid: str, right: str, widths: list[int]) -> str:
    return left + mid.join("─" * (width + 2) for width in widths) + right


def _path(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("path") or "")
    return "" if value is None else str(value)


def _delta(event: dict[str, Any]) -> str:
    if event.get("binary"):
        return "binary"
    additions = event.get("additions")
    deletions = event.get("deletions")
    if additions is None or deletions is None:
        return ""
    return f"+{additions}/-{deletions}"


def _short_time(value: Any) -> str:
    """UTC calendar date. The full timestamp stays in JSON and Markdown."""
    if not isinstance(value, str) or len(value) < 10:
        return "" if value is None else str(value)
    return value[:10]


def _percent(value: Any) -> str:
    if value is None:
        return ""
    number = float(value)
    if number == int(number):
        return f"{int(number)}%"
    return f"{number:.2f}%"


def _commas(value: int) -> str:
    return f"{int(value):,}"


def _visible(text: str) -> int:
    return len(_ANSI.sub("", text))
