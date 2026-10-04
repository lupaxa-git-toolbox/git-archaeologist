"""Temporary Git repositories with explicit identity, dates, and branches."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def git(repo: Path, *args: str, date: str = "2024-06-01T00:00:00Z") -> None:
    """Run Git inside a fixture. Global config and the clock are not consulted."""
    env = os.environ.copy()
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_AUTHOR_NAME"] = "Ada Lovelace"
    env["GIT_AUTHOR_EMAIL"] = "ada@example.com"
    env["GIT_COMMITTER_NAME"] = "Ada Lovelace"
    env["GIT_COMMITTER_EMAIL"] = "ada@example.com"
    env["GIT_AUTHOR_DATE"] = date
    env["GIT_COMMITTER_DATE"] = date
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


def init_repo(path: Path, branch: str = "topic") -> Path:
    """Create an empty repository on an explicit branch name."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", branch)
    git(path, "config", "user.name", "Ada Lovelace")
    git(path, "config", "user.email", "ada@example.com")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "config", "tag.gpgSign", "false")
    return path


def commit_file(repo: Path, name: str, content: str, message: str, date: str) -> None:
    """Write one file and commit it at an explicit committer time."""
    target = repo / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    git(repo, "add", "--", name, date=date)
    git(repo, "commit", "-q", "-m", message, date=date)
