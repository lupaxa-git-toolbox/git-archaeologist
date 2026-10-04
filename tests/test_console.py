"""Console tables stay plain unless colour is requested."""

from __future__ import annotations

from pathlib import Path

from lupaxa.git_archaeologist.options import Request
from lupaxa.git_archaeologist.render import render


def _report() -> dict:
    return {
        "tool_version": "0.0.0",
        "command": "contributors",
        "repository": {"display_name": "demo", "repository_state": "normal"},
        "scope": {
            "target_id": "6641bef03fc456f383cfdeec58ac6702cdaca270",
            "selected_commits": 6,
            "reachable_commits": 6,
        },
        "analysis": {},
        "data": {
            "returned": 1,
            "total": 1,
            "truncated": False,
            "rows": [
                {
                    "canonical_name": "Ada Lovelace",
                    "canonical_email": "ada@example.com",
                    "commits": 6,
                    "percentage": 100.0,
                    "first_selected": {"committer_at": "2024-01-15T10:00:00Z"},
                    "last_selected": {"committer_at": "2025-04-01T08:00:00Z"},
                }
            ],
        },
        "warnings": [],
    }


def _request(*, color: bool) -> Request:
    return Request(
        command="contributors", fmt="console", repository=Path("."), color=color, width=80
    )


def test_plain_console_is_a_table_without_ansi(capsys) -> None:
    render(_report(), _request(color=False))
    output = capsys.readouterr().out
    assert "\x1b[" not in output
    assert "╭" in output
    assert "Ada Lovelace" in output
    assert "100%" in output
    assert "6641bef03fc4" in output


def test_colour_uses_ansi_and_json_does_not(capsys) -> None:
    render(_report(), _request(color=True))
    coloured = capsys.readouterr().out
    assert "\x1b[" in coloured
    assert "Ada Lovelace" in coloured
    request = _request(color=True)
    request.fmt = "json"
    render(_report(), request)
    assert "\x1b[" not in capsys.readouterr().out


def test_long_paths_are_truncated_visibly() -> None:
    from lupaxa.git_archaeologist.console import render_table

    lines = render_table(
        ["Path", "Touches"],
        [["src/" + ("very-long-name/" * 8) + "parser.py", "3"]],
        aligns=["left", "right"],
        width=40,
    )
    body = next(line for line in lines if "parser.py" in line or "..." in line)
    assert "..." in body
