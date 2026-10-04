"""Behaviour checked against fixture repositories."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from fixtures import commit_file, git, init_repo

from lupaxa.git_archaeologist.cli import main


def _run(repo: Path, *args: str) -> tuple[int, str, str]:
    code = main([*args, "--quiet", "--no-color", str(repo)])
    return code, "", ""


def _json(repo: Path, *args: str, capsys) -> dict:
    code = main([*args, "--format", "json", "--quiet", "--no-color", str(repo)])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "\x1b" not in captured.out
    return json.loads(captured.out)


def test_empty_repository_is_a_successful_report(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "empty", branch="topic")
    report = _json(repo, "dig", capsys=capsys)
    assert report["status"] == "ok"
    assert report["repository"]["repository_state"] == "empty"
    assert report["scope"]["target_id"] is None


def test_one_commit_contributors_and_files(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "one", branch="topic")
    commit_file(repo, "src/parser.py", "print(1)\n", "add parser", "2024-03-01T00:00:00Z")
    report = _json(repo, "contributors", capsys=capsys)
    assert report["scope"]["reachable_commits"] == 1
    assert report["data"]["rows"][0]["canonical_email"] == "ada@example.com"
    assert report["data"]["rows"][0]["commits"] == 1
    files = _json(repo, "files", capsys=capsys)
    assert files["data"]["rows"][0]["path"]["path"] == "src/parser.py"
    assert files["data"]["rows"][0]["origin_status"] == "confirmed"
    assert files["data"]["rows"][0]["lifetime_touches"] == 1


def test_merge_is_excluded_from_churn(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "merge", branch="topic")
    commit_file(repo, "keep.txt", "a\n", "base", "2024-01-01T00:00:00Z")
    git(repo, "checkout", "-q", "-b", "side")
    commit_file(repo, "side.txt", "side\n", "side", "2024-02-01T00:00:00Z")
    git(repo, "checkout", "-q", "topic")
    commit_file(repo, "keep.txt", "ab\n", "topic edit", "2024-03-01T00:00:00Z")
    git(repo, "merge", "--no-ff", "-m", "merge side", "side", date="2024-04-01T00:00:00Z")
    report = _json(repo, "timeline", "--group", "year", capsys=capsys)
    assert report["data"]["totals"]["commits"] == 4
    assert report["data"]["totals"]["merges"] == 1
    assert report["data"]["totals"]["added"] == 2
    churn = _json(repo, "churn", capsys=capsys)
    assert sum(row["churn"] for row in churn["data"]["rows"]) == 4


def test_rename_is_one_identity(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "rename", branch="topic")
    commit_file(repo, "old.txt", "alpha\n", "add", "2024-01-01T00:00:00Z")
    git(repo, "mv", "old.txt", "new.txt", date="2024-02-01T00:00:00Z")
    git(repo, "commit", "-q", "-m", "rename", date="2024-02-01T00:00:00Z")
    history = _json(repo, "history", "new.txt", capsys=capsys)
    assert history["data"]["origin_status"] == "confirmed"
    assert history["data"]["events_total"] == 2
    statuses = [event["status"] for event in history["data"]["events"]]
    assert "R" in statuses
    extinct = _json(repo, "extinct", "--kind", "files", capsys=capsys)
    paths = [row["path"]["path"] for row in extinct["data"]["rows"]]
    assert "old.txt" in paths


def test_committed_mailmap_beats_the_worktree(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "mail", branch="topic")
    mail = "Canonical <canonical@example.com> Ada Lovelace <ada@example.com>\n"
    commit_file(repo, ".mailmap", mail, "map", "2024-01-01T00:00:00Z")
    (repo / ".mailmap").write_text(
        "Ada Lovelace <ada@example.com> Worktree <worktree@example.com>\n",
        encoding="utf-8",
    )
    report = _json(repo, "contributors", capsys=capsys)
    assert report["data"]["rows"][0]["canonical_email"] == "canonical@example.com"
    assert report["analysis"]["mailmap"]["status"] == "applied"


def test_dirty_worktree_does_not_change_the_target(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "dirty", branch="topic")
    commit_file(repo, "file.txt", "one\n", "add", "2024-01-01T00:00:00Z")
    first = _json(repo, "files", capsys=capsys)
    (repo / "file.txt").write_text("dirty\n", encoding="utf-8")
    (repo / "untracked.txt").write_text("nope\n", encoding="utf-8")
    second = _json(repo, "files", capsys=capsys)
    assert first == second


def test_json_is_stable(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "stable", branch="topic")
    commit_file(repo, "file.txt", "one\n", "add", "2024-01-01T00:00:00Z")
    code = main(["contributors", "--format", "json", "--quiet", str(repo)])
    first = capsys.readouterr().out
    assert code == 0
    code = main(["contributors", "--format", "json", "--quiet", str(repo)])
    second = capsys.readouterr().out
    assert code == 0
    assert first == second


def test_shared_blob_is_counted_once(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "blobs", branch="topic")
    commit_file(repo, "a.txt", "same\n", "a", "2024-01-01T00:00:00Z")
    git(repo, "checkout", "-q", "HEAD", "--", "a.txt", date="2024-01-02T00:00:00Z")
    target = repo / "b.txt"
    target.write_bytes((repo / "a.txt").read_bytes())
    git(repo, "add", "--", "b.txt", date="2024-01-02T00:00:00Z")
    git(repo, "commit", "-q", "-m", "copy", date="2024-01-02T00:00:00Z")
    report = _json(repo, "blobs", "--min-size", "0", capsys=capsys)
    text_rows = [row for row in report["data"]["rows"] if row["path_count"] == 2]
    assert len(text_rows) == 1
    assert text_rows[0]["presence"] == "present_in_target"
    assert report["data"]["total_bytes"] == sum(row["size"] for row in report["data"]["rows"])


def test_directory_peak_comes_from_snapshots(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "dirs", branch="topic")
    commit_file(repo, "dir/a.txt", "a\n", "a", "2024-01-01T00:00:00Z")
    commit_file(repo, "dir/b.txt", "b\n", "b", "2024-02-01T00:00:00Z")
    git(repo, "rm", "-q", "dir/a.txt", date="2024-03-01T00:00:00Z")
    git(repo, "rm", "-q", "dir/b.txt", date="2024-03-01T00:00:00Z")
    git(repo, "commit", "-q", "-m", "remove dir", date="2024-03-01T00:00:00Z")
    report = _json(repo, "extinct", "--kind", "directories", capsys=capsys)
    directory = next(row for row in report["data"]["rows"] if row["path"]["path"] == "dir")
    assert directory["peak_descendants"] == 2
    assert directory["inferred_from"] == "tracked_paths"
    assert directory["lifetime_touches"] == 3
    assert directory["lifetime_contributors"] == 1
    assert directory["absence_reason"] == "observed_deletion"
    assert directory["removals"][0]["reason"] == "observed_deletion"
    assert directory["removals"][0]["commit"]


def test_sentinel_hook_does_not_run(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "safe", branch="topic")
    commit_file(repo, "file.txt", "one\n", "add", "2024-01-01T00:00:00Z")
    sentinel = tmp_path / "hook-ran"
    hook = repo / ".git" / "hooks" / "post-commit"
    hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
    hook.chmod(0o755)
    before = _inventory(repo)
    assert main(["dig", "--quick", "--quiet", str(repo)]) == 0
    capsys.readouterr()
    assert not sentinel.exists()
    assert _inventory(repo) == before


def test_revision_range_selects_commits_between_refs(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "range", branch="topic")
    commit_file(repo, "file.txt", "one\n", "v1", "2024-01-01T00:00:00Z")
    git(repo, "tag", "v1")
    commit_file(repo, "file.txt", "two\n", "v2", "2024-02-01T00:00:00Z")
    git(repo, "tag", "v2")
    commit_file(repo, "file.txt", "three\n", "v3", "2024-03-01T00:00:00Z")
    report = _json(repo, "churn", "--since-rev", "v1", "--until-rev", "v2", capsys=capsys)
    assert report["scope"]["window"] == "revision"
    assert report["scope"]["selected_commits"] == 1
    assert sum(row["churn"] for row in report["data"]["rows"]) == 2


def test_until_rev_must_be_an_ancestor(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "ancestor", branch="topic")
    commit_file(repo, "keep.txt", "a\n", "base", "2024-01-01T00:00:00Z")
    git(repo, "checkout", "-q", "-b", "side")
    commit_file(repo, "side.txt", "side\n", "side", "2024-02-01T00:00:00Z")
    git(repo, "checkout", "-q", "topic")
    code = main(
        ["churn", "--until-rev", "side", "--format", "json", "--quiet", "--no-color", str(repo)]
    )
    captured = capsys.readouterr()
    assert code == 4
    assert "ancestor" in captured.out


def test_path_limits_ranked_commands(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "scoped", branch="topic")
    commit_file(repo, "src/api/a.txt", "a\n", "api", "2024-01-01T00:00:00Z")
    commit_file(repo, "src/web/b.txt", "b\n", "web", "2024-02-01T00:00:00Z")
    churn = _json(repo, "churn", "--path", "src/api", capsys=capsys)
    assert [row["path"]["path"] for row in churn["data"]["rows"]] == ["src/api/a.txt"]
    people = _json(repo, "contributors", "--path", "src/api", capsys=capsys)
    assert people["data"]["selected_commits"] == 1
    files = _json(repo, "files", "--path", "src/api", capsys=capsys)
    assert [row["path"]["path"] for row in files["data"]["rows"]] == ["src/api/a.txt"]


def test_copy_keeps_its_source(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "copied", branch="topic")
    commit_file(repo, "a.txt", "same\n", "add", "2024-01-01T00:00:00Z")
    (repo / "b.txt").write_text("same\n", encoding="utf-8")
    git(repo, "add", "--", "b.txt", date="2024-02-01T00:00:00Z")
    git(repo, "commit", "-q", "-m", "copy", date="2024-02-01T00:00:00Z")
    history = _json(repo, "history", "b.txt", capsys=capsys)
    copied = [event for event in history["data"]["events"] if event["status"] == "C"]
    assert copied
    assert copied[0]["old_path"]["path"] == "a.txt"
    plain = _json(repo, "history", "--no-copies", "b.txt", capsys=capsys)
    assert all(event["status"] != "C" for event in plain["data"]["events"])


def test_rename_threshold_can_keep_a_rewritten_file(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "threshold", branch="topic")
    original = "".join(f"line {index}\n" for index in range(10))
    rewritten = "".join(
        f"line {index}\n" if index < 4 else f"changed {index}\n" for index in range(10)
    )
    commit_file(repo, "old.txt", original, "add", "2024-01-01T00:00:00Z")
    (repo / "old.txt").write_text(rewritten, encoding="utf-8")
    git(repo, "add", "--", "old.txt", date="2024-02-01T00:00:00Z")
    git(repo, "mv", "old.txt", "new.txt", date="2024-02-01T00:00:00Z")
    git(repo, "commit", "-q", "-m", "rewrite", date="2024-02-01T00:00:00Z")
    strict = _json(repo, "history", "new.txt", capsys=capsys)
    assert "R" not in [event["status"] for event in strict["data"]["events"]]
    loose = _json(repo, "history", "--rename-threshold", "20", "new.txt", capsys=capsys)
    assert "R" in [event["status"] for event in loose["data"]["events"]]


def test_merge_resolution_counts_as_churn(tmp_path: Path, capsys) -> None:
    repo = init_repo(tmp_path / "resolved", branch="topic")
    commit_file(repo, "keep.txt", "base\n", "base", "2024-01-01T00:00:00Z")
    git(repo, "checkout", "-q", "-b", "side")
    commit_file(repo, "keep.txt", "side\n", "side", "2024-02-01T00:00:00Z")
    git(repo, "checkout", "-q", "topic")
    commit_file(repo, "keep.txt", "topic\n", "topic", "2024-03-01T00:00:00Z")
    conflict = git(repo, "merge", "--no-ff", "--no-commit", "side", check=False)
    assert conflict.returncode != 0
    (repo / "keep.txt").write_text("resolved\n", encoding="utf-8")
    git(repo, "add", "--", "keep.txt", date="2024-04-01T00:00:00Z")
    git(repo, "commit", "-q", "-m", "resolve", date="2024-04-01T00:00:00Z")
    churn = _json(repo, "churn", capsys=capsys)
    assert sum(row["churn"] for row in churn["data"]["rows"]) == 7
    history = _json(repo, "history", "keep.txt", capsys=capsys)
    assert any(event["status"] == "M" for event in history["data"]["events"])


def _inventory(repo: Path) -> str:
    env = os.environ.copy()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    parts = []
    for args in (
        ["status", "--porcelain=v1", "-z"],
        ["rev-parse", "HEAD"],
        ["config", "--local", "--list"],
        ["rev-list", "--objects", "--all"],
    ):
        completed = subprocess.run(
            ["git", *args],
            cwd=repo,
            env=env,
            check=True,
            capture_output=True,
        )
        parts.append(completed.stdout)
    return b"\n".join(parts).decode("utf-8", "replace")
