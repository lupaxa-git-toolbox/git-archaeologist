"""Command line for Git Archaeologist."""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any, NoReturn

from lupaxa.git_archaeologist.errors import ToolError
from lupaxa.git_archaeologist.gitio import GitRunner, Repository
from lupaxa.git_archaeologist.options import Request
from lupaxa.git_archaeologist.render import render, render_error
from lupaxa.git_archaeologist.reports import build_report
from lupaxa.git_archaeologist.text import parse_size, parse_user_time
from lupaxa.git_archaeologist.version import get_version

_COMMANDS = (
    "dig",
    "timeline",
    "contributors",
    "survivors",
    "extinct",
    "files",
    "history",
    "churn",
    "blobs",
    "evolution",
)
_GLOBAL_VALUES = {"--format", "--branch", "--revision", "--since", "--until", "--limit"}
_GLOBAL_FLAGS = {"--quiet", "--verbose", "--no-color", "--version", "--help", "-h"}
_ROOT_HELP = """\
Git Archaeologist reads a local Git repository and reports how it evolved.

Usage:
  git-archaeologist [GLOBAL_OPTIONS] COMMAND [COMMAND_OPTIONS] [REPOSITORY]
  git-archaeologist [GLOBAL_OPTIONS] history [COMMAND_OPTIONS] PATH [REPOSITORY]

Commands:
  dig            Overview and small ranked findings
  timeline       Activity buckets over committer time
  contributors   Author activity, using the committed .mailmap when present
  survivors      Oldest paths still in the target tree
  extinct        Paths and directory prefixes absent from the target
  files          Statistics for target files
  history        Biography of one repository-relative path
  churn          Recorded text changes by file or directory
  blobs          Reachable historical blob sizes
  evolution      First-parent tree snapshots

Global options:
  --format console|json|markdown   Default: console
  --branch NAME                    Exact local branch, no tag or remote fallback
  --revision REV                   One commit-ish. Mutually exclusive with --branch
  --since DATE                     Inclusive committer-time lower bound
  --until DATE                     Exclusive upper bound. A date covers that whole day
  --limit N                        Ranked rows. 0 means unlimited. Default: 20
                                   dig uses 5 unless you pass --limit
  --quiet                          Hide progress and stderr warnings
  --verbose                        Phase diagnostics on stderr
  --no-color                       Disable console styling
  --help                           Show help and exit 0
  --version                        Show the version and exit 0

The default target is HEAD, including a detached HEAD. No command is implied.
Dates are YYYY-MM-DD or RFC 3339 with an explicit offset, in UTC.
Merge commits count as activity and are excluded from file change and churn events.
The tool is read-only: it does not fetch, checkout, or write into the repository.
Reports are written to stdout.

Exit status: 0 complete, 1 internal, 2 usage, 3 repository or Git, 4 unresolved
request, 5 partial report, 130 interrupted.
"""


class _Parser(argparse.ArgumentParser):
    """Usage errors are one line on stderr and exit 2."""

    def error(self, message: str) -> NoReturn:
        raise ToolError("USAGE", message, 2)


def main(argv: list[str] | None = None) -> int:
    """Run one command. Returns a process exit status."""
    args = list(sys.argv[1:] if argv is None else argv)
    fmt = "console"
    try:
        if _wants_version(args):
            sys.stdout.write(get_version() + "\n")
            return 0
        command, global_tokens, command_tokens = _partition(args)
        fmt = _format_from(global_tokens)
        if command is None:
            sys.stdout.write(_ROOT_HELP)
            return 0 if _has_help(global_tokens) else 2
        if command not in _COMMANDS:
            raise ToolError("USAGE", f"Unknown command {command!r}.", 2)
        if _has_help(global_tokens) or _has_help(command_tokens):
            sys.stdout.write(_command_help(command))
            return 0
        request = _request(command, global_tokens, command_tokens, fmt)
        return _execute(request)
    except ToolError as exc:
        _emit_tool_error(exc, fmt, command if "command" in locals() else None)
        return exc.exit_code
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        sys.stderr.write("Interrupted.\n")
        return 130
    except Exception as exc:
        if "--verbose" in args:
            traceback.print_exc()
        else:
            sys.stderr.write(f"Error: {exc}\n")
        return 1


def _emit_tool_error(exc: ToolError, fmt: str, command: str | None) -> None:
    if fmt != "json":
        sys.stderr.write(f"Error: {exc.message}\n")
        return
    report = {
        "analysis": None,
        "command": command,
        "data": None,
        "errors": [{"code": exc.code, "context": exc.context, "message": exc.message}],
        "repository": None,
        "schema_version": "1.0",
        "scope": None,
        "status": "error",
        "tool_version": get_version(),
        "warnings": [],
    }
    render_error(report, "json")


def _execute(request: Request) -> int:
    runner = GitRunner()
    try:
        if not request.quiet:
            print("phase: validation", file=sys.stderr)
        runner.require_version()
        repo = Repository(runner, request.repository)
        state, _branch, head_oid = repo.resolve_head()
        if request.branch:
            target = repo.resolve_branch(request.branch)
        elif request.revision:
            target = repo.peel_commit(request.revision)
        elif state == "unborn":
            kind = "unborn_head" if repo.has_other_commits() else "empty"
            render(_bare_report(repo, request, kind), request)
            return 0
        elif head_oid is None:
            raise ToolError(
                "GIT_FAILED",
                "HEAD does not resolve to a commit in this repository.",
                3,
            )
        else:
            target = head_oid
        if not request.quiet:
            print("phase: commit metadata", file=sys.stderr)
        repo.prepare_target(target)
        if not request.quiet:
            print("phase: analysis", file=sys.stderr)
        report = build_report(repo, request, target)
        repo.recheck_target(target)
        if not request.quiet:
            print("phase: rendering", file=sys.stderr)
        for warning in report["warnings"]:
            if not request.quiet:
                print(f"warning: {warning['code']}: {warning['message']}", file=sys.stderr)
        render(report, request)
        return 5 if report["status"] == "partial" else 0
    finally:
        runner.close()


def _bare_report(repo: Repository, request: Request, kind: str) -> dict[str, Any]:
    message = (
        "The repository has no commits."
        if kind == "empty"
        else "HEAD does not point at a commit. Pass --branch or --revision to select one."
    )
    _state, branch, oid = repo.resolve_head()
    branches, tags = repo.local_ref_counts()
    return {
        "analysis": {
            "history_complete": True,
            "mailmap": {"source": None, "status": "absent"},
            "merge_policy": "exclude_merge_file_changes",
            "sections": {},
        },
        "command": request.command,
        "data": {"message": message},
        "errors": [],
        "repository": {
            "bare": repo.bare,
            "common_dir": str(repo.common_dir),
            "display_name": repo.display_name,
            "git_dir": str(repo.git_dir),
            "head": {"branch": branch, "oid": oid, "state": _state},
            "local_branch_count": branches,
            "local_tag_count": tags,
            "object_format": "sha1",
            "repository_state": kind,
            "root": None if repo.root is None else str(repo.root),
            "shallow": repo.shallow,
        },
        "schema_version": "1.0",
        "scope": {
            "reachable_commits": 0,
            "selected_commits": 0,
            "selector_kind": "head",
            "selector_value": "HEAD",
            "since": None,
            "target_id": None,
            "until": None,
        },
        "status": "ok",
        "tool_version": get_version(),
        "warnings": [],
    }


def _exit_code(exc: SystemExit) -> int:
    return exc.code if isinstance(exc.code, int) else 2


def _request(
    command: str, global_tokens: list[str], command_tokens: list[str], fmt: str
) -> Request:
    global_args = _parse_globals(global_tokens)
    parser = _command_parser(command)
    try:
        parsed, unknown = parser.parse_known_args(command_tokens)
    except SystemExit as exc:
        raise ToolError("USAGE", "Invalid command options. See --help.", _exit_code(exc)) from exc
    if unknown:
        raise ToolError("USAGE", f"Unsupported option {unknown[0]!r}.", 2)
    if global_args.branch and global_args.revision:
        raise ToolError("USAGE", "--branch and --revision are mutually exclusive.", 2)
    if global_args.quiet and global_args.verbose:
        raise ToolError("USAGE", "--quiet and --verbose are mutually exclusive.", 2)
    since = _bound(global_args.since, until=False) if global_args.since else None
    until = _bound(global_args.until, until=True) if global_args.until else None
    if since is not None and until is not None and since >= until:
        raise ToolError("USAGE", "The requested time interval is empty or inverted.", 2)
    limit = 20
    explicit = False
    if global_args.limit is not None:
        limit = _limit(global_args.limit)
        explicit = True
    repository = _repository(getattr(parsed, "repository", None))
    path = None
    path_text = getattr(parsed, "path", None)
    if command == "history":
        if path_text is None:
            raise ToolError("USAGE", "history requires a repository-relative path.", 2)
        path = _encode_path(path_text)
    depth = getattr(parsed, "depth", 1)
    if command == "churn" and getattr(parsed, "by", "files") == "directories" and depth < 1:
        raise ToolError("USAGE", "--depth must be 1 or greater.", 2)
    depth_flag = any(token == "--depth" or token.startswith("--depth=") for token in command_tokens)
    directories = getattr(parsed, "by", "files") == "directories"
    if command == "churn" and not directories and depth != 1 and depth_flag:
        raise ToolError("USAGE", "--depth is only valid with --by directories.", 2)
    snapshots = getattr(parsed, "max_snapshots", 120)
    if command == "evolution" and snapshots < 2:
        raise ToolError("USAGE", "--max-snapshots must be at least 2.", 2)
    min_size = 10 * 1024 * 1024
    if getattr(parsed, "min_size", None):
        try:
            min_size = parse_size(parsed.min_size)
        except ValueError as exc:
            raise ToolError("USAGE", str(exc), 2) from exc
    color = (
        fmt == "console"
        and not global_args.no_color
        and sys.stdout.isatty()
        and "NO_COLOR" not in os.environ
    )
    width = shutil.get_terminal_size(fallback=(80, 24)).columns if sys.stdout.isatty() else 80
    return Request(
        command=command,
        fmt=fmt,
        repository=repository,
        branch=global_args.branch,
        revision=global_args.revision,
        since=None if since is None else int(since.timestamp()),
        until=None if until is None else int(until.timestamp()),
        since_text=global_args.since,
        until_text=global_args.until,
        limit=limit,
        limit_explicit=explicit,
        quiet=global_args.quiet,
        verbose=global_args.verbose,
        color=color,
        quick=getattr(parsed, "quick", False),
        group=getattr(parsed, "group", "auto"),
        sort=getattr(parsed, "sort", "") or "",
        no_mailmap=getattr(parsed, "no_mailmap", False),
        no_renames=getattr(parsed, "no_renames", False),
        kind=getattr(parsed, "kind", "all"),
        by=getattr(parsed, "by", "files"),
        depth=depth,
        path=path,
        path_text=path_text,
        incarnation=getattr(parsed, "incarnation", None),
        events=getattr(parsed, "events", False),
        min_size=min_size,
        deleted=getattr(parsed, "deleted", False),
        lines=getattr(parsed, "lines", False),
        max_snapshots=snapshots,
        width=width,
    )


def _parse_globals(tokens: list[str]) -> argparse.Namespace:
    parser = _Parser(prog="git-archaeologist", add_help=False)
    parser.add_argument("--format", choices=("console", "json", "markdown"), default="console")
    parser.add_argument("--branch")
    parser.add_argument("--revision")
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("--limit")
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    try:
        return parser.parse_args(tokens)
    except SystemExit as exc:
        raise ToolError("USAGE", "Invalid global options. See --help.", _exit_code(exc)) from exc


def _command_parser(command: str) -> _Parser:
    parser = _Parser(prog=f"git-archaeologist {command}", add_help=False)
    if command == "history":
        parser.add_argument("path")
        parser.add_argument("repository", nargs="?")
        parser.add_argument("--no-renames", action="store_true")
        parser.add_argument("--incarnation")
        parser.add_argument("--events", action="store_true")
        return parser
    parser.add_argument("repository", nargs="?")
    if command == "dig":
        parser.add_argument("--quick", action="store_true")
    elif command == "timeline":
        parser.add_argument("--group", choices=("auto", "day", "month", "year"), default="auto")
    elif command == "contributors":
        parser.add_argument(
            "--sort",
            choices=("commits", "first", "last", "name"),
            default="commits",
        )
        parser.add_argument("--no-mailmap", action="store_true")
    elif command == "survivors":
        parser.add_argument("--sort", choices=("age", "commits", "path"), default="age")
        parser.add_argument("--no-renames", action="store_true")
    elif command == "extinct":
        parser.add_argument("--kind", choices=("files", "directories", "all"), default="all")
        parser.add_argument("--sort", choices=("last", "age", "commits", "path"), default="last")
    elif command == "files":
        parser.add_argument(
            "--sort",
            choices=("age", "commits", "contributors", "churn", "size", "path"),
            default="commits",
        )
        parser.add_argument("--no-renames", action="store_true")
    elif command == "churn":
        parser.add_argument("--by", choices=("files", "directories"), default="files")
        parser.add_argument("--depth", type=int, default=1)
        parser.add_argument(
            "--sort",
            choices=("churn", "commits", "contributors", "path"),
            default="churn",
        )
        parser.add_argument("--no-renames", action="store_true")
    elif command == "blobs":
        parser.add_argument("--min-size", dest="min_size")
        parser.add_argument("--deleted", action="store_true")
        parser.add_argument("--sort", choices=("size", "path"), default="size")
    elif command == "evolution":
        parser.add_argument("--group", choices=("auto", "day", "month", "year"), default="auto")
        parser.add_argument("--lines", action="store_true")
        parser.add_argument("--max-snapshots", dest="max_snapshots", type=int, default=120)
    return parser


def _command_help(command: str) -> str:
    extra = {
        "dig": "dig [--quick] [REPOSITORY]\n\n--quick skips rename, churn, extinct, and blob scans.\n",
        "timeline": "timeline [--group auto|day|month|year] [REPOSITORY]\n",
        "contributors": "contributors [--sort commits|first|last|name] [--no-mailmap] [REPOSITORY]\n",
        "survivors": "survivors [--sort age|commits|path] [--no-renames] [REPOSITORY]\n",
        "extinct": "extinct [--kind files|directories|all] [--sort last|age|commits|path] [REPOSITORY]\n",
        "files": "files [--sort age|commits|contributors|churn|size|path] [--no-renames] [REPOSITORY]\n",
        "history": (
            "history [--no-renames] [--incarnation COMMIT] [--events] PATH [REPOSITORY]\n\n"
            "PATH is repository-root-relative. Directories are rejected.\n"
            "Rename detection uses a 50% threshold and a candidate limit of 1000.\n"
        ),
        "churn": "churn [--by files|directories] [--depth N] [--sort churn|commits|contributors|path] [REPOSITORY]\n",
        "blobs": (
            "blobs [--min-size SIZE] [--deleted] [--sort size|path] [REPOSITORY]\n\n"
            "Sizes are logical blob bytes. --deleted means the blob is absent from the target,\n"
            "including older versions of paths that still exist. The default threshold is 10MiB.\n"
        ),
        "evolution": (
            "evolution [--group auto|day|month|year] [--lines] [--max-snapshots N] [REPOSITORY]\n\n"
            "Snapshots follow the first-parent chain. --lines counts text lines and skips\n"
            "binaries, symlinks, gitlinks, and recognised LFS pointers.\n"
        ),
    }
    return (
        f"git-archaeologist {extra[command]}\n"
        "Global options work before or after the command. "
        "The default target is HEAD. This tool only reads the local repository.\n"
    )


def _partition(argv: list[str]) -> tuple[str | None, list[str], list[str]]:
    global_tokens: list[str] = []
    command_tokens: list[str] = []
    command: str | None = None
    ended = False
    index = 0
    while index < len(argv):
        token = argv[index]
        if not ended and token == "--":
            ended = True
            command_tokens.append(token)
            index += 1
            continue
        if not ended:
            name, inline = _global_token(token)
            if name is not None:
                global_tokens.append(token if inline is None else f"{name}={inline}")
                if inline is None and name in _GLOBAL_VALUES:
                    index += 1
                    if index >= len(argv):
                        raise ToolError("USAGE", f"{name} needs a value.", 2)
                    global_tokens.append(argv[index])
                index += 1
                continue
        if command is None and not token.startswith("-"):
            command = token
            index += 1
            continue
        command_tokens.append(token)
        index += 1
    return command, global_tokens, command_tokens


def _global_token(token: str) -> tuple[str | None, str | None]:
    if token in _GLOBAL_VALUES or token in _GLOBAL_FLAGS:
        return token, None
    if token.startswith("--") and "=" in token:
        name, value = token.split("=", 1)
        if name in _GLOBAL_VALUES:
            return name, value
    return None, None


def _format_from(tokens: list[str]) -> str:
    fmt = "console"
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "--format" and index + 1 < len(tokens):
            fmt = tokens[index + 1]
            index += 2
            continue
        if token.startswith("--format="):
            fmt = token.split("=", 1)[1]
        index += 1
    if fmt not in {"console", "json", "markdown"}:
        raise ToolError("USAGE", f"Unknown format {fmt!r}.", 2)
    return fmt


def _wants_version(argv: list[str]) -> bool:
    return "--version" in argv


def _has_help(tokens: list[str]) -> bool:
    return "--help" in tokens or "-h" in tokens


def _bound(text: str, *, until: bool) -> Any:
    try:
        return parse_user_time(text, until=until)
    except ValueError as exc:
        raise ToolError("USAGE", str(exc), 2) from exc


def _limit(text: str) -> int:
    if not text.isdigit():
        raise ToolError("USAGE", f"Invalid limit {text!r}.", 2)
    return int(text)


def _repository(value: str | None) -> Path:
    raw = Path.cwd() if value is None else Path(value).expanduser()
    if not raw.is_absolute():
        raw = Path.cwd() / raw
    try:
        resolved = raw.resolve(strict=False)
    except OSError as exc:
        raise ToolError("NOT_REPOSITORY", str(exc), 3) from exc
    if not resolved.is_dir():
        raise ToolError(
            "NOT_REPOSITORY",
            f"Repository path is not an existing directory: {resolved}",
            3,
        )
    return resolved


def _encode_path(text: str) -> bytes:
    encoded = os.fsencode(text)
    if not encoded or encoded.startswith((b"/", b"\\")) or b".." in encoded.split(b"/"):
        raise ToolError("USAGE", "Pass one repository-relative path without '..' components.", 2)
    return encoded
