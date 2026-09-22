# merge-tool

A small script for scoping out a merge before you do it. It looks at a set of
branches against a target branch, does a trial merge in a throwaway worktree
(never touching your real branches), and sorts any conflicts into two
buckets: `machine` (safe to auto-resolve) and `human` (needs a person to look
at it).

## What it does

1. **understand_branches** — for each branch, checks whether it shares
   history with the target (`git merge-base`), what files it touches, and
   where its changed files overlap with other branches' changed files.
2. **sort_conflicts** — sets up a temporary git worktree at the target's tip
   and merges each branch into it one at a time (`git merge --no-ff
   --no-commit`). Clean merges get committed so later branches are checked
   against the combined result. Conflicting merges are aborted; nothing is
   ever left half-merged. Branches with no shared history are skipped.
3. **bucket_conflict** — for each conflicted file, decides `machine` if it's
   one of a small set of config files that are safe to combine
   (`.env.example`, `.gitignore`, `README.md`) or if both sides made the
   identical change, otherwise `human`.

The worktree and any temp branches are always cleaned up, even on error.

## How to run it

```
python3 merge_tool.py <repo> <target> <branch> [<branch> ...] [-o branches.json]
```

Example:

```
python3 merge_tool.py /path/to/repo main feat/a feat/b feat/c
```

## What it outputs

- **branches.json** (path set with `-o`, default `branches.json`) — the full
  `understand_branches` result: base commit, shares_history, files touched,
  and overlap, keyed by branch name.
- A printed report on stdout from `sort_conflicts`: for each branch, either
  `clean`, `skipped (no shared history)`, or one line per conflicted file
  with its bucket (`machine`/`human`) and a short reason. This report is not
  currently written to a file, just printed.

## Test result

`test_replay.py` replays the bucketing rule against a real conflict log
(`conflicts.jsonl` from the `mimic-LLM-service` repo's `staging` branch, 14
recorded conflicts from an actual merge) and compares the tool's predicted
bucket against what actually happened (`needs_human`).

Result: **12 of 14 matched.** The 2 that didn't were both cases where the
tool predicted `human` but the conflict was actually resolved without one:

- `tests/test_character_turn.py` — combined both sides' tests, reusing a
  resolution pattern that had already been reviewed once.
- `tests/test_track_b_retrieval_gate.py` — kept one side's version and only
  touched an import path.

In both cases a person still made the call at the time; the tool just isn't
aware of "we've resolved this exact shape before" or "only an import path
differs." Its rule set only knows two safe patterns (named config files,
identical content) and defaults to `human` for everything else, so it erred
toward flagging things as needing a person rather than auto-resolving
something it shouldn't have. No case went the other way.
