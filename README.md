# merge-tool

Trial-merges a set of branches into a target branch, each in a throwaway git worktree.
It reports which branches merge cleanly and which conflict, and fixes the conflicts that are safe for a machine.
The rest are left for a person, with an optional AI explanation. Your existing branches are not changed.

## Files

- `merge_tool.py` - the core logic and the command line: branch inspection, trial merge, conflict sorting.
- `app.py` - the Streamlit app.
- `ai_resolve.py` - the AI helper: `explain_conflict` asks the model about a conflict, `check_merged` sanity-checks its proposed file.
- `requirements.txt` - Python packages.
- `tests/test_replay.py` - replays the conflict-sorting rule against a recorded conflict log.
- `tests/test_ai_resolve.py` - one AI call on the `notes.txt` conflict in the demo repo.
- `tests/test_ai_replay.py` - runs the AI on every conflict in the log that needs a person, writes `ai_replay_results.jsonl`.
- `tests/show_replay.py` - turns `ai_replay_results.jsonl` into one markdown file per conflict in `replay_review/`.

`branches.json`, `ai_replay_results.jsonl` and `replay_review/` are generated and git-ignored.

## Setup

```
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file in the repo root:

```
OPENAI_API_KEY=your key
OPENAI_MODEL=gpt-4o-mini
```

`OPENAI_MODEL` is optional (default `gpt-4o-mini`). `OPENAI_BASE_URL` is also read if set.
Only the AI features need the key.

## Run

App:

```
streamlit run app.py
```

Enter the repo path, press "Load branches", pick the target and the branches to merge, then press Run.

Command line (no AI):

```
python3 merge_tool.py <repo> <target> <branch> [<branch> ...] [-o branches.json]
```

It prints one report line per branch and writes the branch inspection to `branches.json`.

## Tests

Run from the repo root.

```
python3 tests/test_replay.py       # no API calls
python3 tests/test_ai_resolve.py   # one API call
python3 tests/test_ai_replay.py    # about ten API calls
python3 tests/show_replay.py       # reads ai_replay_results.jsonl, writes replay_review/
```

The tests read fixed paths on this machine: the conflict log in `~/Documents/mimic-LLM-service/merge-tool/`, the repo `~/Documents/mimic-LLM-service`, and the demo repo `~/Documents/merge-tool-demo/repo`.

## Statuses

- **clean** - the branch merged with no conflicts.
- **auto-fixed** - it had conflicts, but every one was safe for a machine (a config file where both sides can be combined, or both sides made the same change). They were fixed and the merge committed.
- **needs human** - at least one conflicted file needs a person. Machine-safe files are already fixed. The rest still contain conflict markers.
- **skipped** - the branch shares no history with the target, so it was not merged.

## Review worktrees

A branch that needs a person keeps its worktree. It lives in the system temp directory (`merge_tool_<random>/wt`) on a branch named `mergetool-review-<branch>`. The app shows the path under each conflicted branch.

If a review worktree from an earlier run exists for a selected branch, the app stops with a message instead of running. Tick "Clean up previous review worktrees" to remove the old worktree and branch and run fresh. A worktree with uncommitted edits is not removed: it is kept with a warning, so finish or discard those edits first. The command line has no cleanup option; use `git worktree remove` and `git branch -D`.
