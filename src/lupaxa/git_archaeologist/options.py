"""Parsed command-line choices shared by analysis and rendering."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Request:
    """One invocation after usage errors have been rejected."""

    command: str
    fmt: str
    repository: Path
    branch: str | None = None
    revision: str | None = None
    since: int | None = None
    until: int | None = None
    since_text: str | None = None
    until_text: str | None = None
    limit: int = 20
    limit_explicit: bool = False
    quiet: bool = False
    verbose: bool = False
    color: bool = False
    quick: bool = False
    group: str = "auto"
    sort: str = ""
    no_mailmap: bool = False
    no_renames: bool = False
    kind: str = "all"
    by: str = "files"
    depth: int = 1
    path: bytes | None = None
    path_text: str | None = None
    incarnation: str | None = None
    events: bool = False
    min_size: int = 10 * 1024 * 1024
    deleted: bool = False
    lines: bool = False
    max_snapshots: int = 120
    width: int = 80
