"""Read-only Git subprocess access and repository discovery."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lupaxa.git_archaeologist.errors import ToolError
from lupaxa.git_archaeologist.text import mode_kind

_KEEP_ENV = {
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "TMPDIR",
    "TMP",
    "TEMP",
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
    "TERM",
}
_STDERR_LIMIT = 8192


@dataclass
class GitOutput:
    """Captured output from one Git invocation."""

    args: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass
class Leaf:
    """One tracked leaf in a tree."""

    mode: str
    kind: str
    oid: str
    path: bytes


@dataclass
class Commit:
    """Commit metadata. Names use surrogateescape so unusual bytes survive."""

    oid: str
    tree: str
    parents: tuple[str, ...]
    author_name: str
    author_email: str
    author_ts: int
    committer_name: str
    committer_email: str
    committer_ts: int


@dataclass
class Change:
    """One eligible file change. Merge commits do not produce these."""

    commit: str
    status: str
    similarity: int | None
    old_path: bytes | None
    new_path: bytes | None
    old_mode: str
    new_mode: str
    old_oid: str
    new_oid: str
    additions: int | None
    deletions: int | None
    binary: bool

    @property
    def path(self) -> bytes:
        """Path this event is known by after the change."""
        return self.new_path if self.new_path is not None else self.old_path or b""


@dataclass
class WarningNote:
    """A consolidated analysis warning."""

    code: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


def _sanitized_env() -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items() if key in _KEEP_ENV or key.startswith("LC_")
    }
    env.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_PAGER": "cat",
            "PAGER": "cat",
        }
    )
    return env


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


class GitRunner:
    """Invoke the installed Git executable with a locked-down environment."""

    def __init__(self) -> None:
        git = shutil.which("git")
        if git is None:
            raise ToolError(
                "GIT_NOT_FOUND",
                "Git was not found on PATH. Install Git 2.30 or newer and retry.",
                3,
            )
        self.git = git
        self.temp = tempfile.TemporaryDirectory(prefix="git-archaeologist-")
        self.calls: list[tuple[str, ...]] = []
        self._env = _sanitized_env()
        self._prefix = [
            "-c",
            "core.pager=cat",
            "-c",
            "color.ui=false",
            "-c",
            "color.diff=false",
            "-c",
            "advice.detachedHead=false",
            "-c",
            "maintenance.auto=false",
            "-c",
            "gc.auto=0",
            "-c",
            "fetch.recurseSubmodules=false",
            "-c",
            "submodule.recurse=false",
            "-c",
            f"core.hooksPath={self.temp.name}",
            "-c",
            "core.fsmonitor=",
            "-c",
            "core.attributesFile=/dev/null",
            "-c",
            "diff.external=",
            "-c",
            "filter.lfs.required=false",
            "-c",
            "filter.lfs.process=",
            "-c",
            "filter.lfs.smudge=",
            "-c",
            "filter.lfs.clean=",
            "--no-pager",
            "--no-replace-objects",
            "--no-optional-locks",
            "--literal-pathspecs",
        ]

    def close(self) -> None:
        """Remove temporary hook and mailmap files."""
        self.temp.cleanup()

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
        check: bool = True,
        stdin: bytes | None = None,
    ) -> GitOutput:
        """Run Git. Arguments are passed as an array and never through a shell."""
        command = [self.git, *self._prefix, *args]
        self.calls.append(tuple(args))
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=self._env,
                input=stdin,
                capture_output=True,
                shell=False,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ToolError(
                "GIT_NOT_FOUND",
                "Git was not found on PATH. Install Git 2.30 or newer and retry.",
                3,
            ) from exc
        stderr = completed.stderr[-_STDERR_LIMIT:]
        output = GitOutput(tuple(args), completed.returncode, completed.stdout, stderr)
        if check and completed.returncode != 0:
            raise _failure(args, output)
        return output

    def require_version(self) -> str:
        """Reject Git older than 2.30."""
        output = self.run(["version"])
        text = output.stdout.decode("utf-8", "replace").strip()
        parts = text.split()
        number = ""
        for part in parts:
            if part and part[0].isdigit():
                number = part
                break
        pieces = number.split(".")
        if len(pieces) < 2:
            raise ToolError("GIT_UNSUPPORTED", f"Could not read the Git version from {text!r}.", 3)
        major, minor = int(pieces[0]), int(pieces[1])
        if (major, minor) < (2, 30):
            raise ToolError(
                "GIT_UNSUPPORTED",
                f"Git {number} is older than the required 2.30 baseline.",
                3,
            )
        return text


def _failure(args: Sequence[str], output: GitOutput) -> ToolError:
    text = output.stderr.decode("utf-8", "replace").strip()
    lowered = text.lower()
    if "dubious ownership" in lowered or "safe.directory" in lowered:
        return ToolError(
            "UNSAFE_REPOSITORY",
            "Git refused this repository because of ownership protection. "
            "Resolve it outside Git Archaeologist. This tool does not add safe.directory.",
            3,
            {"stderr": text[:500]},
        )
    if "not a git repository" in lowered:
        return ToolError(
            "NOT_REPOSITORY",
            "Git could not find a repository at that path.",
            3,
            {"stderr": text[:500]},
        )
    if "missing" in lowered or "bad object" in lowered or "unable to read" in lowered:
        return ToolError(
            "MISSING_OBJECT",
            "A required Git object is missing. Git Archaeologist does not fetch objects.",
            3,
            {"operation": " ".join(args[:6]), "stderr": text[:500]},
        )
    detail = text or f"Git exited {output.returncode}"
    return ToolError(
        "GIT_FAILED",
        f"Git command failed: {detail}",
        3,
        {"operation": " ".join(args[:8])},
    )


def _parse_ident(raw: bytes) -> tuple[str, str, int]:
    end = raw.rfind(b">")
    start = raw.rfind(b" <", 0, end)
    if start < 0 or end < 0:
        raise ToolError("GIT_FAILED", "Git returned a commit identity that could not be parsed.", 3)
    name = _decode(raw[:start])
    email = _decode(raw[start + 2 : end])
    tail = raw[end + 1 :].split()
    if not tail:
        raise ToolError("GIT_FAILED", "Git returned a commit identity without a timestamp.", 3)
    return name, email, int(tail[0])


def _parse_commit(oid: str, body: bytes) -> Commit:
    header = body.split(b"\n\n", 1)[0]
    tree = ""
    parents: list[str] = []
    author = ("", "", 0)
    committer = ("", "", 0)
    for line in header.split(b"\n"):
        if line.startswith(b"tree "):
            tree = line[5:].decode("ascii")
        elif line.startswith(b"parent "):
            parents.append(line[7:].decode("ascii"))
        elif line.startswith(b"author "):
            author = _parse_ident(line[7:])
        elif line.startswith(b"committer "):
            committer = _parse_ident(line[10:])
    if not tree or not committer[0] and committer[2] == 0:
        raise ToolError("GIT_FAILED", f"Commit {oid} is missing tree or committer metadata.", 3)
    return Commit(
        oid=oid,
        tree=tree,
        parents=tuple(parents),
        author_name=author[0],
        author_email=author[1],
        author_ts=author[2],
        committer_name=committer[0],
        committer_email=committer[1],
        committer_ts=committer[2],
    )


def _parse_batch(blob: bytes) -> list[tuple[str, bytes]]:
    records: list[tuple[str, bytes]] = []
    index = 0
    while index < len(blob):
        newline = blob.find(b"\n", index)
        if newline < 0:
            if blob[index:].strip():
                raise ToolError("GIT_FAILED", "Git object stream ended inside a header.", 3)
            break
        header = blob[index:newline]
        index = newline + 1
        if header.endswith(b" missing"):
            oid = header.split()[0].decode("ascii", "replace")
            raise ToolError(
                "MISSING_OBJECT",
                f"Object {oid} is missing. Git Archaeologist does not fetch objects.",
                3,
                {"object": oid},
            )
        parts = header.split()
        if len(parts) < 3:
            raise ToolError("GIT_FAILED", "Git object stream returned a short header.", 3)
        oid = parts[0].decode("ascii")
        try:
            size = int(parts[-1])
        except ValueError as exc:
            raise ToolError("GIT_FAILED", "Git object stream returned a bad size.", 3) from exc
        body = blob[index : index + size]
        if len(body) != size:
            raise ToolError("GIT_FAILED", "Git object stream ended before the object body.", 3)
        index += size
        if index < len(blob) and blob[index : index + 1] == b"\n":
            index += 1
        records.append((oid, body))
    return records


def _parse_tree(body: bytes, digest_length: int) -> list[tuple[str, bytes, str]]:
    entries: list[tuple[str, bytes, str]] = []
    index = 0
    while index < len(body):
        space = body.find(b" ", index)
        if space < 0:
            raise ToolError("GIT_FAILED", "Git tree stream ended inside a mode.", 3)
        nul = body.find(b"\0", space + 1)
        if nul < 0:
            raise ToolError("GIT_FAILED", "Git tree stream ended inside a name.", 3)
        mode = body[index:space].decode("ascii")
        name = body[space + 1 : nul]
        end = nul + 1 + digest_length
        if end > len(body):
            raise ToolError("GIT_FAILED", "Git tree stream ended inside an object ID.", 3)
        oid = body[nul + 1 : end].hex()
        entries.append((mode, name, oid))
        index = end
    return entries


def _normalize_mode(mode: str) -> str:
    if mode == "40000":
        return "040000"
    return mode.zfill(6)


def _parse_raw(blob: bytes) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    index = 0
    length = len(blob)
    while index < length:
        nul = blob.find(b"\0", index)
        if nul < 0:
            if blob[index:].strip(b"\n"):
                raise ToolError("GIT_FAILED", "Git name-status stream ended early.", 3)
            break
        meta = blob[index:nul]
        index = nul + 1
        if not meta or meta.startswith(b"commit "):
            continue
        if not meta.startswith(b":"):
            raise ToolError("GIT_FAILED", "Git diff stream returned an unexpected record.", 3)
        parts = meta[1:].split()
        if len(parts) < 5:
            raise ToolError("GIT_FAILED", "Git diff stream returned a short record.", 3)
        status = parts[4].decode("ascii")
        letter = status[0]
        similarity = int(status[1:]) if letter in {"R", "C"} and status[1:].isdigit() else None
        path_count = 2 if letter in {"R", "C"} else 1
        paths: list[bytes] = []
        for _ in range(path_count):
            nul = blob.find(b"\0", index)
            if nul < 0:
                raise ToolError("GIT_FAILED", "Git diff stream ended inside a path.", 3)
            paths.append(blob[index:nul])
            index = nul + 1
        old_path = paths[0] if letter in {"R", "C", "D"} else (None if letter == "A" else paths[0])
        new_path: bytes | None
        if letter in {"R", "C"}:
            old_path = paths[0]
            new_path = paths[1]
        elif letter == "D":
            old_path = paths[0]
            new_path = None
        elif letter == "A":
            old_path = None
            new_path = paths[0]
        else:
            old_path = paths[0]
            new_path = paths[0]
        records.append(
            {
                "status": "R" if letter == "R" else letter,
                "similarity": similarity,
                "old_mode": _normalize_mode(parts[0].decode("ascii")),
                "new_mode": _normalize_mode(parts[1].decode("ascii")),
                "old_oid": parts[2].decode("ascii"),
                "new_oid": parts[3].decode("ascii"),
                "old_path": old_path,
                "new_path": new_path,
            }
        )
    return records


def _read_until(blob: bytes, index: int, separator: bytes) -> tuple[bytes, int]:
    end = blob.find(separator, index)
    if end < 0:
        raise ToolError("GIT_FAILED", "Git numstat stream ended early.", 3)
    return blob[index:end], end + len(separator)


def _apply_numstat(blob: bytes, records: list[dict[str, Any]]) -> None:
    """Numstat keeps tabs between counts. ``-z`` terminates paths with NUL."""
    index = 0
    for record in records:
        added, index = _read_until(blob, index, b"\t")
        deleted, index = _read_until(blob, index, b"\t")
        if record["status"] in {"R", "C"}:
            if blob[index : index + 1] == b"\0":
                index += 1
            old, index = _read_until(blob, index, b"\0")
            new, index = _read_until(blob, index, b"\0")
            paths = [old, new]
            expected = [record["old_path"], record["new_path"]]
        else:
            path, index = _read_until(blob, index, b"\0")
            paths = [path]
            expected = [record["old_path"] if record["status"] == "D" else record["new_path"]]
        if paths != expected:
            raise ToolError(
                "GIT_FAILED",
                "Git numstat paths did not match the raw diff record order.",
                3,
            )
        binary = added == b"-" or deleted == b"-"
        record["binary"] = binary
        record["additions"] = None if binary else int(added)
        record["deletions"] = None if binary else int(deleted)
    if index != len(blob):
        raise ToolError("GIT_FAILED", "Git numstat stream contained unused records.", 3)


class Repository:
    """A discovered repository and the history reachable from one commit."""

    def __init__(self, runner: GitRunner, start: Path) -> None:
        self.runner = runner
        self.start = start
        self.warnings: list[WarningNote] = []
        self.rename_complete = True
        self.mailmap_status = "absent"
        self.mailmap_source: str | None = None
        self._mailmap: dict[tuple[str, str], tuple[str, str]] = {}
        self._mailmap_file: Path | None = None
        self._mailmap_repo: Path | None = None
        self._trees: dict[str, tuple[Leaf, ...]] = {}
        self._tree_bodies: dict[str, bytes] = {}
        self.commits: dict[str, Commit] = {}
        self.order: list[str] = []
        self.changes: list[Change] = []
        self._changes_mode: bool | None = None
        self.shallow_boundaries: set[str] = set()
        self.empty_tree = ""
        self.digest_length = 20
        self._discover()

    def _git(self, args: Sequence[str], **kwargs: Any) -> GitOutput:
        return self.runner.run(args, cwd=self.start, **kwargs)

    def _discover(self) -> None:
        inside = self._git(["rev-parse", "--is-inside-work-tree"], check=False)
        bare = self._git(["rev-parse", "--is-bare-repository"], check=False)
        if inside.returncode != 0 and bare.returncode != 0:
            message = inside.stderr.decode("utf-8", "replace")
            if "dubious ownership" in message.lower():
                raise _failure(["rev-parse"], inside)
            raise ToolError(
                "NOT_REPOSITORY",
                "Git could not find a repository at that path.",
                3,
            )
        self.bare = bare.stdout.strip() == b"true"
        self.git_dir = Path(self._git(["rev-parse", "--absolute-git-dir"]).stdout.decode().strip())
        common = self._git(["rev-parse", "--git-common-dir"]).stdout.decode().strip()
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = (self.git_dir.parent / common_path).resolve()
        else:
            common_path = common_path.resolve()
        self.common_dir = common_path
        if self.bare:
            self.root: Path | None = None
            base = self.start.resolve()
            name = base.name
            self.display_name = name.removesuffix(".git") or name
        else:
            top = self._git(["rev-parse", "--show-toplevel"]).stdout.decode().strip()
            self.root = Path(top).resolve()
            self.display_name = self.root.name
        shallow = self._git(["rev-parse", "--is-shallow-repository"]).stdout.strip() == b"true"
        self.shallow = shallow
        if shallow:
            self.warnings.append(
                WarningNote(
                    "SHALLOW_HISTORY",
                    "This repository is shallow. History before the shallow boundary "
                    "was not observed.",
                    {},
                )
            )
        grafts = self.common_dir / "info" / "grafts"
        if grafts.is_file() and grafts.stat().st_size > 0:
            raise ToolError(
                "GIT_FAILED",
                "Legacy graft metadata is present, so raw history cannot be guaranteed. "
                "Remove or replace it outside Git Archaeologist.",
                3,
                {"path": str(grafts)},
            )
        shallow_file = self.common_dir / "shallow"
        if shallow_file.is_file():
            self.shallow_boundaries = {
                line.strip()
                for line in shallow_file.read_text(encoding="ascii", errors="replace").splitlines()
                if line.strip()
            }
        self._neutralize_aliases()

    def _neutralize_aliases(self) -> None:
        listed = self._git(
            ["config", "--local", "--null", "--get-regexp", r"^alias\."], check=False
        )
        if listed.returncode != 0 or not listed.stdout:
            return
        extra: list[str] = []
        for record in listed.stdout.split(b"\0"):
            if not record:
                continue
            key = record.split(b"\n", 1)[0].decode("utf-8", "replace")
            if key.startswith("alias."):
                extra.extend(["-c", f"{key}="])
        if extra:
            self.runner._prefix = extra + self.runner._prefix

    def object_format(self, oid: str) -> str:
        """Return sha1 or sha256 from a full object ID."""
        if len(oid) == 64:
            return "sha256"
        return "sha1"

    def resolve_head(self) -> tuple[str, str | None, str | None]:
        """Return ``(state, branch, oid)``. State is branch, detached, unborn, or missing."""
        symbolic = self._git(["symbolic-ref", "--quiet", "HEAD"], check=False)
        verified = self._git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], check=False)
        branch = None
        if symbolic.returncode == 0:
            ref = symbolic.stdout.decode().strip()
            if ref.startswith("refs/heads/"):
                branch = ref[len("refs/heads/") :]
        if verified.returncode == 0:
            oid = verified.stdout.decode().strip()
            state = "branch" if branch else "detached"
            return state, branch, oid
        if symbolic.returncode == 0:
            return "unborn", branch, None
        return "missing", None, None

    def local_ref_counts(self) -> tuple[int, int]:
        """Return local branch and tag counts. These are inventory, not selected history."""
        branches = self._git(["for-each-ref", "--format=%(refname)", "refs/heads"])
        tags = self._git(["for-each-ref", "--format=%(refname)", "refs/tags"])
        branch_count = len([line for line in branches.stdout.splitlines() if line])
        tag_count = len([line for line in tags.stdout.splitlines() if line])
        return branch_count, tag_count

    def peel_commit(self, revision: str) -> str:
        """Resolve one commit-ish to a full commit ID."""
        if revision.startswith("-"):
            raise ToolError(
                "INVALID_REVISION",
                "Revision arguments must not begin with '-'. Pass '--' only as an option boundary.",
                4,
                {"revision": revision},
            )
        if ".." in revision or " " in revision:
            raise ToolError(
                "INVALID_REVISION",
                "Ranges and multiple revisions are not supported. Pass one commit, tag, or branch.",
                4,
                {"revision": revision},
            )
        verified = self._git(
            ["rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}"],
            check=False,
        )
        if verified.returncode != 0:
            raise ToolError(
                "INVALID_REVISION",
                f"Could not resolve {revision!r} to a commit.",
                4,
                {"revision": revision},
            )
        return verified.stdout.decode().strip()

    def resolve_branch(self, name: str) -> str:
        """Resolve an exact local branch. Tags and remotes are not fallbacks."""
        if name.startswith("-") or not name or "/" in name and name.startswith("-"):
            raise ToolError("INVALID_REVISION", f"Invalid branch name {name!r}.", 4)
        if name.startswith("-"):
            raise ToolError("INVALID_REVISION", f"Invalid branch name {name!r}.", 4)
        ref = f"refs/heads/{name}"
        verified = self._git(
            ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
            check=False,
        )
        if verified.returncode != 0:
            raise ToolError(
                "INVALID_REVISION",
                f"Local branch {name!r} does not resolve to a commit.",
                4,
                {"branch": name},
            )
        return verified.stdout.decode().strip()

    def has_other_commits(self) -> bool:
        """True when any local ref points at a commit."""
        listed = self._git(["rev-list", "--all", "--count"], check=False)
        if listed.returncode != 0:
            return False
        return listed.stdout.strip() not in {b"", b"0"}

    def prepare_target(self, oid: str) -> None:
        """Load commits reachable from ``oid`` and the empty-tree ID."""
        self.digest_length = len(oid) // 2
        empty = self._git(["hash-object", "-t", "tree", "--stdin"], stdin=b"")
        self.empty_tree = empty.stdout.decode().strip()
        self._trees[self.empty_tree] = ()
        listed = self._git(["rev-list", "--topo-order", oid])
        order = [line.decode("ascii") for line in listed.stdout.splitlines() if line]
        if not order:
            raise ToolError("INVALID_REVISION", "The selected revision has no commit.", 4)
        payload = b"".join(item.encode("ascii") + b"\n" for item in order)
        batched = self._git(["cat-file", "--batch"], stdin=payload)
        bodies = dict(_parse_batch(batched.stdout))
        for item in order:
            if item not in bodies:
                raise ToolError("MISSING_OBJECT", f"Commit {item} disappeared during analysis.", 3)
            self.commits[item] = _parse_commit(item, bodies[item])
        self.order = order
        self._note_non_monotonic()

    def _note_non_monotonic(self) -> None:
        for oid, commit in self.commits.items():
            for parent in commit.parents:
                previous = self.commits.get(parent)
                if previous and commit.committer_ts < previous.committer_ts:
                    self.warnings.append(
                        WarningNote(
                            "NON_MONOTONIC_TIME",
                            "Committer timestamps are not monotonic. Ages that would be "
                            "negative are omitted instead of invented.",
                            {"commit": oid, "parent": parent},
                        )
                    )
                    return

    def ensure_changes(self, *, renames: bool) -> None:
        """Diff every non-merge commit once. Shallow boundaries are not treated as roots."""
        if self._changes_mode is renames:
            return
        self.changes = []
        self._changes_mode = renames
        rename_args = ["-M50%", "--find-renames=50%", "-l1000"] if renames else ["--no-renames"]
        for oid in reversed(self.order):
            commit = self.commits[oid]
            if len(commit.parents) > 1:
                continue
            if commit.parents and commit.parents[0] not in self.commits:
                self.shallow_boundaries.add(oid)
                continue
            if oid in self.shallow_boundaries:
                continue
            parent_tree = (
                self.empty_tree if not commit.parents else self.commits[commit.parents[0]].tree
            )
            base = ["diff-tree", "-r", "-z", *rename_args, parent_tree, commit.tree]
            raw = self._git(base + ["--raw"])
            num = self._git(base + ["--numstat"])
            self._note_rename_limit(raw.stderr + num.stderr)
            records = _parse_raw(raw.stdout)
            if records:
                _apply_numstat(num.stdout, records)
            else:
                if num.stdout.strip(b"\0"):
                    raise ToolError("GIT_FAILED", "Git returned numstat without a raw diff.", 3)
            for record in records:
                if record["status"] == "C":
                    record["status"] = "A"
                    record["old_path"] = None
                    record["similarity"] = None
                self.changes.append(
                    Change(
                        commit=oid,
                        status=record["status"],
                        similarity=record["similarity"],
                        old_path=record["old_path"],
                        new_path=record["new_path"],
                        old_mode=record["old_mode"],
                        new_mode=record["new_mode"],
                        old_oid=record["old_oid"],
                        new_oid=record["new_oid"],
                        additions=record.get("additions"),
                        deletions=record.get("deletions"),
                        binary=bool(record.get("binary")),
                    )
                )

    def _note_rename_limit(self, stderr: bytes) -> None:
        text = stderr.decode("utf-8", "replace").lower()
        if "rename detection was skipped" in text or "inexact rename" in text:
            self.rename_complete = False
            if not any(item.code == "RENAME_LIMIT" for item in self.warnings):
                self.warnings.append(
                    WarningNote(
                        "RENAME_LIMIT",
                        "Git skipped exhaustive rename detection because the candidate "
                        "limit was exhausted. Rename completeness is false for affected "
                        "comparisons.",
                        {"limit": 1000, "threshold": 50},
                    )
                )

    def tree_leaves(self, tree: str) -> tuple[Leaf, ...]:
        """Return tracked leaves for a tree, loading each Git tree once."""
        if tree in self._trees:
            return self._trees[tree]
        pending = [tree]
        seen = {tree, self.empty_tree}
        while pending:
            payload = b"".join(item.encode("ascii") + b"\n" for item in pending)
            batched = self._git(["cat-file", "--batch"], stdin=payload)
            pending = []
            for oid, body in _parse_batch(batched.stdout):
                self._tree_bodies[oid] = body
                for mode, _name, child in _parse_tree(body, self.digest_length):
                    normalized = _normalize_mode(mode)
                    if (
                        normalized == "040000"
                        and child not in seen
                        and child not in self._tree_bodies
                    ):
                        seen.add(child)
                        pending.append(child)
        self._materialize_tree(tree, b"")
        return self._trees[tree]

    def _materialize_tree(self, tree: str, prefix: bytes) -> tuple[Leaf, ...]:
        cached = self._trees.get(tree)
        if cached is not None and prefix == b"":
            return cached
        if tree == self.empty_tree:
            return ()
        body = self._tree_bodies.get(tree)
        if body is None:
            self.tree_leaves(tree)
            body = self._tree_bodies[tree]
        leaves: list[Leaf] = []
        for mode, name, oid in _parse_tree(body, self.digest_length):
            normalized = _normalize_mode(mode)
            path = name if not prefix else prefix + b"/" + name
            if normalized == "040000":
                leaves.extend(self._materialize_tree(oid, path))
            else:
                leaves.append(
                    Leaf(
                        mode=normalized,
                        kind=mode_kind(normalized),
                        oid=oid,
                        path=path,
                    )
                )
        if prefix == b"":
            self._trees[tree] = tuple(leaves)
        return tuple(leaves)

    def blob_sizes(self, oids: Iterable[str]) -> dict[str, int]:
        """Logical blob sizes via batch metadata. Bodies are not read."""
        unique = [oid for oid in dict.fromkeys(oids) if oid]
        sizes: dict[str, int] = {}
        if not unique:
            return sizes
        payload = b"".join(oid.encode("ascii") + b"\n" for oid in unique)
        output = self._git(
            ["cat-file", "--batch-check=%(objectname) %(objecttype) %(objectsize)"],
            stdin=payload,
        )
        for line in output.stdout.splitlines():
            parts = line.split()
            if len(parts) != 3:
                raise ToolError("GIT_FAILED", "Git blob metadata stream was truncated.", 3)
            oid, kind, size = parts[0].decode("ascii"), parts[1].decode("ascii"), parts[2]
            if kind != "blob":
                continue
            sizes[oid] = int(size)
        return sizes

    def blob_bodies(self, oids: Iterable[str]) -> dict[str, bytes]:
        """Stream requested blob bodies. Used only for line and pointer inspection."""
        unique = list(dict.fromkeys(oids))
        if not unique:
            return {}
        payload = b"".join(oid.encode("ascii") + b"\n" for oid in unique)
        output = self._git(["cat-file", "--batch"], stdin=payload)
        return dict(_parse_batch(output.stdout))

    def load_mailmap(self, tree: str, enabled: bool) -> None:
        """Canonicalize with the target tree ``.mailmap`` blob only."""
        if not enabled:
            self.mailmap_status = "disabled"
            self.mailmap_source = None
            return
        probe = self._git(["cat-file", "-e", f"{tree}:.mailmap"], check=False)
        if probe.returncode != 0:
            self.mailmap_status = "absent"
            self.mailmap_source = None
            return
        blob = self._git(["cat-file", "blob", f"{tree}:.mailmap"], check=False)
        if blob.returncode != 0:
            self._mailmap_fallback("The committed .mailmap blob could not be read.")
            return
        path = Path(self.runner.temp.name) / "mailmap"
        path.write_bytes(blob.stdout)
        scratch = Path(self.runner.temp.name) / "mailmap-repo"
        scratch.mkdir(exist_ok=True)
        self.runner.run(["init", "-q", "-b", "unused"], cwd=scratch)
        self.mailmap_source = "target_tree:.mailmap"
        self._mailmap_file = path
        self._mailmap_repo = scratch
        self.mailmap_status = "applied"

    def _mailmap_fallback(self, message: str) -> None:
        self.mailmap_status = "fallback"
        self.mailmap_source = None
        self._mailmap = {}
        if not any(item.code == "MAILMAP_FALLBACK" for item in self.warnings):
            self.warnings.append(
                WarningNote(
                    "MAILMAP_FALLBACK",
                    message + " Identities are reported raw.",
                    {},
                )
            )

    def canonicalize(self, name: str, email: str) -> tuple[str, str]:
        """Map one raw identity through the committed mailmap, or return it unchanged."""
        if self.mailmap_status != "applied":
            return name, email
        key = (name, email)
        cached = self._mailmap.get(key)
        if cached is not None:
            return cached
        if "\n" in name or "\n" in email or "\r" in name or "\r" in email:
            self._mailmap[key] = key
            return key
        contact = f"{name} <{email}>\n".encode("utf-8", "surrogateescape")
        result = self.runner.run(
            [
                "-c",
                f"mailmap.file={self._mailmap_file}",
                "-c",
                "mailmap.blob=",
                "check-mailmap",
                "--stdin",
            ],
            cwd=self._mailmap_repo,
            stdin=contact,
            check=False,
        )
        if result.returncode != 0:
            self._mailmap_fallback("The committed .mailmap could not be parsed.")
            return name, email
        line = result.stdout.decode("utf-8", "surrogateescape").strip()
        end = line.rfind(">")
        start = line.rfind(" <", 0, end)
        if start < 0 or end < 0:
            self._mailmap_fallback("The committed .mailmap produced an unusable identity.")
            return name, email
        canonical = (line[:start], line[start + 2 : end])
        self._mailmap[key] = canonical
        return canonical

    def recheck_target(self, oid: str) -> None:
        """Fail if the resolved commit disappeared while the report was built."""
        verified = self._git(["cat-file", "-e", f"{oid}^{{commit}}"], check=False)
        if verified.returncode != 0:
            raise ToolError(
                "MISSING_OBJECT",
                "The selected commit disappeared during analysis. "
                "The repository was not locked or modified.",
                3,
                {"object": oid},
            )
