<p align="center">
  <a href="https://github.com/lupaxa-git-toolbox">
    <img src="https://raw.githubusercontent.com/the-lupaxa-project/brand-assets/master/logos/organisations/git-toolbox/readme-logo.png" alt="Git Toolbox" />
  </a>
</p>

<h1 align="center">Git Archaeologist</h1>

Git tells you what your repository is. Git Archaeologist tells you how it
got there.

It reads a local Git repository and reports activity, contributors,
surviving files, vanished paths, file biographies, churn, large historical
blobs, and tree snapshots. The same facts are available as console text,
JSON, or Markdown.

The tool is read-only. It does not fetch, rewrite history, or write reports
into the repository. Reports go to stdout. Redirect them yourself if you
want a file.

The PyPI name is `lupaxa-git-archaeologist`. The import path is
`lupaxa.git_archaeologist`. `lupaxa` is a namespace package. The console
script is `git-archaeologist`.

## Install

```bash
pip install lupaxa-git-archaeologist
```

Requires Python 3.10+ and Git 2.30 or newer. There are no runtime Python
dependencies.

You can also run `python -m lupaxa.git_archaeologist`.

## Commands

No command is implied. `git-archaeologist` with no command prints help and
exits 2. The optional repository argument defaults to the current directory.
Running from a subdirectory still analyses the whole repository. Paths
passed to `history` are relative to the repository root.

The default target is `HEAD`, including a detached HEAD. `--branch` selects
an exact local branch. `--revision` selects one commit, tag, or
remote-tracking ref. Those two options cannot be combined.

| Command        | What it shows                                                    |
| -------------- | ---------------------------------------------------------------- |
| `dig`          | A short overview, then small ranked findings                     |
| `timeline`     | Activity buckets by day, month, or year                          |
| `contributors` | Author activity, using a committed `.mailmap` when one exists    |
| `survivors`    | Oldest paths still present in the target tree                    |
| `extinct`      | Paths and directory prefixes absent from the target tree         |
| `files`        | Statistics for files in the target tree                          |
| `history`      | The biography of one repository-relative path                    |
| `churn`        | Recorded text changes, by file or by directory                   |
| `blobs`        | Reachable historical blob sizes                                  |
| `evolution`    | First-parent snapshots of the tree                               |

## Examples

```bash
git-archaeologist dig
git-archaeologist dig ~/Projects/demo --branch master
git-archaeologist --format json churn --revision release/2.0
git-archaeologist contributors --since 2025-01-01 --until 2025-12-31
git-archaeologist history --events src/grammar.py
git-archaeologist blobs --deleted --min-size 10MiB
git-archaeologist timeline --group year
git-archaeologist evolution --group month --lines
```

`--format` selects `console`, `json`, or `markdown`. Console reports draw
ranked results as tables. Colour is used only when stdout is a terminal,
`NO_COLOR` is unset, and `--no-color` was not passed. JSON and Markdown
never contain ANSI sequences. Console dates are UTC calendar days. JSON
keeps the full timestamp.

`--limit` caps ranked rows. It does not change totals. The default is 20.
`dig` shows 5 rows in each ranked section unless you pass `--limit`.
`dig --quick` skips rename, churn, extinct, and blob scans and marks those
sections skipped.

Phase names are printed on stderr while a command runs. `--quiet` hides
progress and stderr warnings. Warnings that affect the report stay in the
JSON or Markdown output. `--verbose` adds phase diagnostics on stderr.
`--quiet` and `--verbose` cannot be combined.

> **Note:** Merge commits count as activity, and their file diffs are not
> included in churn. Every report names this policy. Dates use committer
> time in UTC. A date-only `--since` starts at midnight UTC. A date-only
> `--until` runs through the end of that day.

## Sample Output

The samples below come from a six-commit repository. It adds `src/parser.py`,
extends it, adds `src/lexer.py`, renames the parser to `src/grammar.py`, then
adds and deletes `old.txt`. The display name in a real run is the worktree
directory name. The tool version below is the package version at the time of
the run.

### Contributors

```text
Git Archaeologist 0.0.0
Repository  demo
State       normal
Target      6641bef03fc4
Commits     6 selected, 6 reachable
Merge commits count toward activity and are excluded from file change and churn events.

Contributors
Showing 1 of 1
╭──────────────┬─────────────────┬─────────┬──────┬────────────┬────────────╮
│ Name         │ Email           │ Commits │    % │ First      │ Last       │
├──────────────┼─────────────────┼─────────┼──────┼────────────┼────────────┤
│ Ada Lovelace │ ada@example.com │       6 │ 100% │ 2024-01-15 │ 2025-04-01 │
╰──────────────┴─────────────────┴─────────┴──────┴────────────┴────────────╯
```

Canonical names come from the `.mailmap` blob in the selected target tree.
A missing mailmap leaves the raw author identities unchanged. A dirty
worktree mailmap is ignored.

### Churn

```text
Churn
Showing 4 of 4
╭────────────────┬───────┬───────┬─────────┬─────────┬─────────┬────────╮
│ Path           │ Churn │ Added │ Deleted │ Commits │ Authors │ Binary │
├────────────────┼───────┼───────┼─────────┼─────────┼─────────┼────────┤
│ src/grammar.py │     5 │     5 │       0 │       3 │       1 │      0 │
│ old.txt        │     2 │     1 │       1 │       2 │       1 │      0 │
│ README.md      │     1 │     1 │       0 │       1 │       1 │      0 │
│ src/lexer.py   │     1 │     1 │       0 │       1 │       1 │      0 │
╰────────────────┴───────┴───────┴─────────┴─────────┴─────────┴────────╯
```

Churn is numeric additions plus deletions. The renamed parser is reported
under its target path, `src/grammar.py`. Binary changes are counted
separately and are not treated as zero-length text edits.

### Timeline

`timeline` is a table of buckets, including empty months between the first
and last selected commit. Columns are bucket, commits, authors, added,
deleted, renames, and churn. `dig` uses the same header, then one table
each for survivors, the most changed files, extinct paths, large blobs,
and discoveries. `blobs` stays empty on this repository because the default
threshold is 10 MiB.

### File Biography

`history --events src/grammar.py` follows the rename:

```text
History
Origin confirmed  Events 3
╭──────────────┬────────────┬────────┬─────────────────────────────────┬───────╮
│ Commit       │ When       │ Status │ Path                            │   +/- │
├──────────────┼────────────┼────────┼─────────────────────────────────┼───────┤
│ 2ea96a9a7202 │ 2024-01-15 │ A      │ src/parser.py                   │ +2/-0 │
│ 46eecb288582 │ 2024-06-01 │ M      │ src/parser.py                   │ +3/-0 │
│ a47913a78f68 │ 2025-02-01 │ R      │ src/parser.py -> src/grammar.py │ +0/-0 │
╰──────────────┴────────────┴────────┴─────────────────────────────────┴───────╯
```

JSON always includes the event list. This excerpt is the rename event:

```json
{
  "additions": 0,
  "deletions": 0,
  "new_path": {
    "path": "src/grammar.py",
    "path_bytes_base64": "c3JjL2dyYW1tYXIucHk="
  },
  "old_path": {
    "path": "src/parser.py",
    "path_bytes_base64": "c3JjL3BhcnNlci5weQ=="
  },
  "similarity": 100,
  "status": "R"
}
```

`path_bytes_base64` is the reversible path. `origin_status` for this file is
`confirmed`. A shallow boundary or several possible introductions is reported
as `boundary` or `ambiguous` instead of an invented creation date.

## Formats

JSON is one object plus a trailing newline. Keys are sorted. Repeated runs on
an unchanged repository are byte-stable: there is no generated-at timestamp.
Field names for schema 1.0 are described in
`schema/git-archaeologist-1.0.schema.json`.

```json
{
  "schema_version": "1.0",
  "tool_version": "0.0.0",
  "command": "contributors",
  "status": "ok",
  "data": {
    "returned": 1,
    "total": 1,
    "truncated": false
  },
  "warnings": [],
  "errors": []
}
```

Markdown is a standalone report with a scope section, the command results,
and any warnings. Filenames are escaped in table cells.

## Exit Status

| Exit | Meaning                                                          |
| ---- | ---------------------------------------------------------------- |
| 0    | Complete, including an empty repository or an empty date window  |
| 1    | Internal failure                                                 |
| 2    | Usage error, including a bad date, size, or option combination   |
| 3    | Git is missing, too old, or the path is not a usable repository  |
| 4    | The branch, revision, or path could not be resolved              |
| 5    | An optional section failed and a partial report was written      |
| 130  | Interrupted                                                      |

<a href="https://github.com/the-lupaxa-project">
    <img src="https://raw.githubusercontent.com/the-lupaxa-project/brand-assets/master/logos/components/footer-for-child-orgs.svg" alt="The Lupaxa Project Footer" width="100%" />
</a>
