"""Command line contracts that do not need a repository."""

from __future__ import annotations

from lupaxa.git_archaeologist.cli import main
from lupaxa.git_archaeologist.version import get_version


def test_missing_command_prints_help_and_exits_2(capsys) -> None:
    assert main([]) == 2
    output = capsys.readouterr().out
    assert "git-archaeologist" in output
    assert "dig" in output


def test_help_and_version_do_not_need_a_repository(capsys) -> None:
    assert main(["--help"]) == 0
    assert main(["dig", "--help"]) == 0
    assert main(["--version"]) == 0
    assert get_version() in capsys.readouterr().out


def test_unknown_command_is_usage(capsys) -> None:
    assert main(["nope"]) == 2
    assert "Unknown command" in capsys.readouterr().err


def test_conflicting_selectors(capsys) -> None:
    assert main(["--branch", "topic", "--revision", "HEAD", "dig"]) == 2
    assert "mutually exclusive" in capsys.readouterr().err
