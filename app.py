import difflib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from merge_tool import fatal_line, is_untouched, sort_conflicts, understand_branches

COLORS = {
    "clean": "green",
    "auto-fixed": "dodgerblue",
    "needs human": "orange",
    "skipped": "grey",
}


def git(repo, *args):
    # run a git command in the repo and return the finished process
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True)


def check_inputs(repo, target, branches):
    # return an error message, or None if the repo and every branch look fine
    if not os.path.isdir(repo):
        return f"Repo path does not exist: {repo}"
    if git(repo, "rev-parse", "--is-inside-work-tree").returncode != 0:
        return f"Not a git repository: {repo}"
    if not branches:
        return "Enter at least one branch name."
    for name in [target] + branches:
        if git(repo, "rev-parse", "--verify", "--quiet", f"{name}^{{commit}}").returncode != 0:
            return f"Branch does not exist: {name}"
    return None


def find_leftovers(repo, branches):
    # find old mergetool-review-<branch> branches/worktrees
    # returns {review branch: worktree path, or None if it has no worktree}
    worktrees = {}
    path = None
    for line in git(repo, "worktree", "list", "--porcelain").stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/"):
            worktrees[line[len("branch refs/heads/"):]] = path

    leftovers = {}
    for branch in branches:
        review = f"mergetool-review-{branch}"
        has_branch = git(repo, "branch", "--list", review).stdout.strip()
        if has_branch or review in worktrees:
            leftovers[review] = worktrees.get(review)
    return leftovers


def remove_leftovers(repo, leftovers):
    # delete old review worktrees and branches that nobody touched, plus orphaned temp branches
    # returns (what was removed, review branches kept because of edits, other problems)
    removed, edited, problems = [], [], []
    kept = set()
    for review, path in leftovers.items():
        if not path:
            continue
        # edited, or no snapshot to compare with: keep worktree and branch
        if not is_untouched(path):
            edited.append(review)
            kept.add(review)
            continue
        # --force is needed: an untouched conflicted merge still counts as dirty to git
        result = git(repo, "worktree", "remove", "--force", path)
        if result.returncode != 0:
            problems.append(f"Could not remove {review}: {fatal_line(result.stderr)}")
            kept.add(review)
        else:
            removed.append(f"worktree {path}")
    # forget worktrees already deleted on disk, so their branches can be deleted
    git(repo, "worktree", "prune")
    for review in leftovers:
        if review in kept:
            continue
        git(repo, "branch", "-D", review)
        removed.append(f"branch {review}")
    # temp branches whose worktree folder is gone are throwaway
    orphans = git(
        repo, "branch", "--list", "merge-tool-tmp-*", "--format=%(refname:short) %(worktreepath)"
    ).stdout.splitlines()
    dropped = 0
    for line in orphans:
        name, _, worktree = line.partition(" ")
        if name and not worktree.strip() and git(repo, "branch", "-D", name).returncode == 0:
            dropped += 1
    if dropped:
        removed.append(f"{dropped} old temp branches")
    return removed, edited, problems


def status_of(files):
    # files is None (skipped), [] (clean) or a list of conflict entries
    if files is None:
        return "skipped"
    if not files:
        return "clean"
    if any(f["bucket"] == "human" for f in files):
        return "needs human"
    return "auto-fixed"


def reason_of(status, files):
    if status == "skipped":
        return "no shared history"
    if status == "clean":
        return "merged without conflicts"
    return "; ".join(f"{f['file']}: {f['comment']}" for f in files)


def color_status(value):
    return f"color: {COLORS[value]}; font-weight: bold"


def show_overview(data):
    rows = []
    for branch, info in data.items():
        rows.append(
            {
                "branch": branch,
                "shares history": info["shares_history"],
                "files touched": len(info["files_touched"]),
                "overlaps with": ", ".join(info["overlap"]) or "-",
            }
        )
    st.dataframe(
        pd.DataFrame(rows),
        hide_index=True,
        width="stretch",
        column_config={
            "branch": st.column_config.TextColumn("Branch"),
            "shares history": st.column_config.CheckboxColumn("Shares history"),
            "files touched": st.column_config.NumberColumn("Files touched", format="%d"),
            "overlaps with": st.column_config.TextColumn("Overlaps with"),
        },
    )


def show_results(conflicts):
    rows = []
    for branch, files in conflicts.items():
        status = status_of(files)
        rows.append(
            {
                "branch": branch,
                "status": status,
                "files": ", ".join(f["file"] for f in files or []) or "-",
                "reason": reason_of(status, files),
            }
        )
    # the status column is coloured text, so it reads like a badge
    table = pd.DataFrame(rows).style.map(color_status, subset=["status"])
    st.dataframe(
        table,
        hide_index=True,
        width="stretch",
        column_config={
            "branch": st.column_config.TextColumn("Branch"),
            "status": st.column_config.TextColumn("Status", width="small"),
            "files": st.column_config.TextColumn("Files"),
            "reason": st.column_config.TextColumn("Reason"),
        },
    )


def show_metrics(conflicts):
    # one tile per status, in a row
    statuses = [status_of(files) for files in conflicts.values()]
    tiles = [("Clean", "clean"), ("Auto-fixed", "auto-fixed"), ("Needs human", "needs human"), ("Skipped", "skipped")]
    for column, (label, status) in zip(st.columns(4), tiles):
        column.metric(label, statuses.count(status))


def show_stage(worktree, stage, path):
    # one side of a conflict (1 = base, 2 = ours, 3 = theirs), or None if that stage is missing
    result = subprocess.run(
        ["git", "-C", worktree, "show", f":{stage}:{path}"],
        capture_output=True,
        text=True,
        errors="replace",
    )
    return result.stdout if result.returncode == 0 else None



def load_versions(worktree, path):
    # base / ours / theirs from the conflict stages (None = that stage is missing)
    return [show_stage(worktree, stage, path) for stage in (1, 2, 3)]


def show_three_way(versions):
    # base / ours / theirs side by side
    if all(v is None for v in versions):
        # stages disappear once the file is resolved and staged
        st.info("Three-way view not available: this file is no longer in a conflicted state.")
        return
    titles = ["Base", "Ours (target)", "Theirs (branch)"]
    for column, title, text in zip(st.columns(3), titles, versions):
        with column:
            st.markdown(f"**{title}**")
            # add/add conflicts have no base, so a missing stage means no file on that side
            st.code(text if text is not None else "(file did not exist)")


def show_raw_markers(worktree, path):
    # the kept worktree holds the file with conflict markers
    try:
        with open(os.path.join(worktree, path), errors="replace") as fh:
            st.code(fh.read())
    except OSError as e:
        st.warning(f"Could not read {path}: {e.strerror}")


def last_subject(worktree, ref):
    # subject line of the last commit on ref, or "unknown"
    result = git(worktree, "log", "-1", "--format=%s", ref)
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else "unknown"


def ask_ai(worktree, target, branch, path, versions):
    # call the AI helper; any failure comes back as ok=False, never an exception
    try:
        from ai_resolve import explain_conflict

        base, ours, theirs = (v or "" for v in versions)
        return explain_conflict(
            path,
            base,
            ours,
            theirs,
            target,
            branch,
            last_subject(worktree, target),
            last_subject(worktree, branch),
        )
    except Exception as e:
        return {"ok": False, "reason": f"could not run the AI helper ({type(e).__name__})"}


def failure_reason(result):
    # short reason for a failed AI call (ai_resolve prints the details in the terminal)
    if result.get("reason"):
        return result["reason"]
    if result.get("raw_text") is None:
        return "no reply from the model (check OPENAI_API_KEY and the connection)"
    return "the reply was not valid JSON"


def show_ai_result(result):
    # the AI's answer in a bordered box, in a fixed order
    with st.container(border=True):
        if not result.get("ok"):
            st.error(f"AI unavailable: {failure_reason(result)}")
            return

        st.markdown("**Explanation**")
        st.write(str(result.get("explanation", "")))
        col_ours, col_theirs = st.columns(2)
        col_ours.info(f"**If we keep only ours**\n\n{result.get('risk_ours', '')}")
        col_theirs.info(f"**If we keep only theirs**\n\n{result.get('risk_theirs', '')}")

        if result.get("recommend"):
            st.markdown(f":blue-background[**AI recommends: {result['recommend']}**]")
        if result.get("port_from_other"):
            st.markdown("**Worth porting from the other side**")
            st.markdown("\n".join(f"- {item}" for item in result["port_from_other"]))

        merged = result.get("merged", "")
        if not merged:
            st.info("AI did not propose a merged file — decide by hand using the explanation above.")
            return
        st.markdown("**Proposed merged file**")
        checks = result.get("checks", [])
        if checks:
            st.error("\n".join(f"- {problem}" for problem in checks))
        else:
            st.success("passed checks")
        st.code(merged)


def is_review_worktree(worktree):
    # only ever write in a worktree whose branch is a mergetool-review-* branch
    head = git(worktree, "symbolic-ref", "--short", "HEAD")
    return head.returncode == 0 and head.stdout.strip().startswith("mergetool-review-")


def write_atomic(worktree, path, text):
    # write to a temp file next to the target, then rename; returns an error message or None
    root = os.path.realpath(worktree)
    full = os.path.realpath(os.path.join(root, path))
    if not full.startswith(root + os.sep):
        return f"{path} is outside the review worktree, nothing written."
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(full), prefix=".merge-tool-")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        if os.path.exists(full):
            shutil.copymode(full, tmp)
        else:
            os.chmod(tmp, 0o644)
        os.replace(tmp, full)
        return None
    except OSError as e:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)
        return f"Could not write {path}: {e.strerror}"


def write_and_stage(worktree, path, text, versions, key):
    # check, write and `git add` one file in the review worktree; returns an error or None
    try:
        from ai_resolve import check_merged
    except Exception:
        return "Could not load the checker, nothing written."
    problems = check_merged(path, text)
    if problems:
        return "Not written:\n" + "\n".join(f"- {p}" for p in problems)
    if not is_review_worktree(worktree):
        return "This is not a review worktree, nothing written."
    error = write_atomic(worktree, path, text)
    if error:
        return error
    added = git(worktree, "add", "--", path)
    if added.returncode != 0:
        lines = added.stderr.strip().splitlines()
        return f"Wrote {path} but git add failed: {lines[0] if lines else 'unknown error'}"
    st.session_state[f"res:{key}"] = "resolved"
    # the conflict stages are gone once staged, so keep the view we showed
    st.session_state[f"view:{key}"] = versions
    return None


PROPOSALS_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proposals.jsonl")


def log_proposal(entry):
    # append one line per decision; a failed write only warns
    try:
        with open(PROPOSALS_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
            fh.flush()
    except OSError as e:
        st.warning(f"Could not write proposals.jsonl: {e.strerror}")


def changed_line_count(before, after):
    # number of lines that differ between two texts
    a, b = before.splitlines(), after.splitlines()
    ops = difflib.SequenceMatcher(None, a, b).get_opcodes()
    return sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in ops if tag != "equal")


def log_decision(key, path, decision, changed_lines):
    # key is "<branch>:<file>"; the rest comes from the session
    result = st.session_state.get(f"ai:{key}", {})
    loaded = st.session_state.get("loaded", {})
    log_proposal(
        {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": os.environ.get("OPENAI_MODEL") or "unknown",
            "repo": os.path.basename(loaded.get("repo", "").rstrip("/")),
            "target": st.session_state.get("last_run", {}).get("target", ""),
            "branch": key[: -len(path) - 1],
            "file": path,
            "recommend": result.get("recommend", ""),
            "checks": result.get("checks", []),
            "decision": decision,
            "changed_lines": changed_lines,
        }
    )


def show_editor(worktree, path, merged, versions, key):
    # the proposed file in a text box; Save checks, writes and stages it
    text = st.text_area("Proposed file (editable)", value=merged, height=400, key=f"text:{key}")
    if st.button("Save", key=f"save:{key}"):
        error = write_and_stage(worktree, path, text, versions, key)
        if error:
            st.error(error)
        else:
            log_decision(key, path, "edit", changed_line_count(merged, text))
            st.rerun()


def show_decision(worktree, path, result, versions, key):
    # Accept / Edit / Reject for a proposed merged file
    state = st.session_state.get(f"res:{key}")
    if state == "resolved":
        st.success("Resolved — staged in the review worktree")
        return
    if state == "rejected":
        st.caption("Rejected — file still has conflict markers; fix it by hand in the review worktree.")
        return

    accept, edit, reject, _ = st.columns([1, 1, 1, 5])
    # Accept needs a clean check result
    if accept.button("Accept", key=f"accept:{key}", type="primary", disabled=bool(result.get("checks"))):
        error = write_and_stage(worktree, path, result["merged"], versions, key)
        if error:
            st.error(error)
        else:
            log_decision(key, path, "accept", 0)
            st.rerun()
    if edit.button("Edit", key=f"edit:{key}"):
        st.session_state[f"editing:{key}"] = True
    if reject.button("Reject", key=f"reject:{key}"):
        st.session_state[f"res:{key}"] = "rejected"
        log_decision(key, path, "reject", None)
        st.rerun()
    if st.session_state.get(f"editing:{key}"):
        show_editor(worktree, path, result["merged"], versions, key)


def ask_about(question, path, versions, target, branch):
    # the AI's plain-text answer to a free-form question, or None if the call failed
    try:
        from ai_resolve import ask_question

        base, ours, theirs = (v or "" for v in versions)
        return ask_question(question, path, base, ours, theirs, target, branch)
    except Exception:
        return None


def show_question(path, versions, target, branch, key):
    # free-form question box; the last question and answer are kept per branch + file
    st.markdown("**Ask a question about this conflict**")
    question = st.text_input(
        "Question",
        placeholder="e.g. what breaks if I take only their side?",
        key=f"q:{key}",
        label_visibility="collapsed",
    )
    if st.button("Ask", key=f"qask:{key}"):
        if not question.strip():
            st.warning("Type a question first.")
        else:
            with st.spinner("Asking AI..."):
                answer = ask_about(question.strip(), path, versions, target, branch)
            st.session_state[f"qa:{key}"] = {"question": question.strip(), "answer": answer}
    saved = st.session_state.get(f"qa:{key}")
    if saved:
        with st.container(border=True):
            st.markdown(f"**Q:** {saved['question']}")
            if saved["answer"] is None:
                st.error("AI unavailable")
            else:
                st.write(saved["answer"])


def show_ai_section(worktree, target, branch, path, versions):
    # "Ask AI" button; the answer is cached per branch + file so reruns don't call the API
    key = f"{branch}:{path}"
    if f"ai:{key}" not in st.session_state:
        if not st.button("Ask AI about this conflict", key=f"ask:{key}", type="primary"):
            return
        with st.spinner("Asking AI..."):
            st.session_state[f"ai:{key}"] = ask_ai(worktree, target, branch, path, versions)
    result = st.session_state[f"ai:{key}"]
    show_ai_result(result)
    if result.get("ok") and result.get("merged"):
        show_decision(worktree, path, result, versions, key)
    show_question(path, versions, target, branch, key)
    if st.session_state.get(f"res:{key}") != "resolved" and st.button("Ask again", key=f"again:{key}"):
        # a new answer starts fresh: forget any earlier decision and edits
        for prefix in ("ai:", "res:", "editing:", "text:"):
            st.session_state.pop(f"{prefix}{key}", None)
        st.rerun()


def show_human_file(f, target, branch):
    # one conflicted file: side-by-side / raw tabs, then the AI helper
    st.markdown(f"**`{f['file']}`** — {f['comment']}")
    key = f"{branch}:{f['file']}"
    versions = st.session_state.get(f"view:{key}")
    if versions is None:
        versions = load_versions(f["review_path"], f["file"])
    side, raw = st.tabs(["Side by side", "Raw markers"])
    with side:
        show_three_way(versions)
    with raw:
        show_raw_markers(f["review_path"], f["file"])
    if not all(v is None for v in versions):
        show_ai_section(f["review_path"], target, branch, f["file"], versions)


def show_branch_card(branch, files, target):
    # one bordered card per conflicted branch
    status = status_of(files)
    human = [f for f in files if f["bucket"] == "human"]
    with st.container(border=True):
        names = ", ".join(f"`{f['file']}`" for f in files)
        st.markdown(f"**{branch}** — {status}: {names}")
        for f in files:
            if f["bucket"] == "machine":
                st.write(f"Auto-fixed `{f['file']}`: {f['comment']}")
        if human:
            st.caption(f"Review worktree kept at: {human[0]['review_path']}")
        for f in human:
            show_human_file(f, target, branch)


def show_conflicts(conflicts, target):
    # needs-human branches first, then auto-fixed ones
    order = {"needs human": 0, "auto-fixed": 1}
    cards = [(b, fs) for b, fs in conflicts.items() if status_of(fs) in order]
    if not cards:
        st.info("No conflicts to review.")
    for branch, files in sorted(cards, key=lambda c: order[status_of(c[1])]):
        show_branch_card(branch, files, target)


def load_branches(repo):
    # local branch names for the repo, returns (names, error)
    repo = os.path.expanduser(repo.strip())
    if not os.path.isdir(repo):
        return None, f"Repo path does not exist: {repo}"
    result = git(repo, "branch", "--format=%(refname:short)")
    if result.returncode != 0:
        lines = result.stderr.strip().splitlines()
        return None, "Could not list branches: " + (lines[0] if lines else "not a git repository")
    names = []
    for name in result.stdout.splitlines():
        name = name.strip()
        # skip detached-HEAD lines and this tool's own temporary branches
        if name and not name.startswith(("(", "merge-tool-tmp-", "mergetool-review-")):
            names.append(name)
    if not names:
        return None, "No branches found in this repo."
    return names, None


def run_merge(repo, target, branches, clean, status):
    # check inputs, clean up if asked, trial-merge; returns (result dict, error message)
    repo = os.path.expanduser(repo.strip())
    error = check_inputs(repo, target, branches)
    if error:
        return None, error

    leftovers = find_leftovers(repo, branches)
    if leftovers and not clean:
        return None, (
            f"A review copy from an earlier run still exists for: {', '.join(leftovers)}. "
            "It may contain a fix someone started. Ticking 'Clean up previous review "
            "worktrees' will delete it and run fresh."
        )
    cleaned = None
    if clean:
        status.update(label="Cleaning up old review copies…")
        removed, edited, problems = remove_leftovers(repo, leftovers)
        if edited:
            names = ", ".join(b.removeprefix("mergetool-review-") for b in edited)
            return None, (
                f"Review copy for {names} has edits. Finish or discard them in the review "
                "worktree, or untick those branches."
            )
        if problems:
            return None, "\n".join(problems)
        cleaned = ", ".join(removed) or "nothing to clean"

    status.update(label="Inspecting branches…")
    data = understand_branches(repo, target, branches)
    # the branches are merged as one chain, so progress can't be reported per branch
    status.update(label=f"Merging {len(branches)} branches…")
    conflicts = sort_conflicts(repo, target, branches, data)
    return {"data": data, "conflicts": conflicts, "target": target, "cleaned": cleaned}, None


def short_error(e):
    # git's fatal:/error: line, else its last line, else the error's own message or type
    stderr = getattr(e, "stderr", None)
    if stderr and stderr.strip():
        return fatal_line(stderr)
    return str(e) or type(e).__name__


def show_sidebar():
    # returns (repo, target, branches to merge, clean, run clicked)
    with st.sidebar:
        repo = st.text_input("Repo path")
        if st.button("Load branches"):
            names, error = load_branches(repo)
            if error:
                st.error(error)
            else:
                st.session_state["loaded"] = {"repo": repo.strip(), "names": names}
                # new widget keys per load: picks for a previous repo are forgotten
                st.session_state["load_id"] = st.session_state.get("load_id", 0) + 1

        loaded = st.session_state.get("loaded")
        ready = bool(loaded) and loaded["repo"] == repo.strip()
        if loaded and not ready:
            st.caption("Repo path changed — load branches again.")
        names = loaded["names"] if ready else []

        # the widget keys stay the same between loads, so the picks stay in session state
        load_id = st.session_state.get("load_id", 0)
        index = (names.index("main") if "main" in names else 0) if names else None
        target = st.selectbox("Target branch", names, index=index, key=f"target:{load_id}", disabled=not ready)
        options = [n for n in names if n != target]
        picks_key = f"to_merge:{load_id}"
        # drop picks that are no longer on offer (e.g. the new target)
        picks = st.session_state.get(picks_key, [])
        if any(b not in options for b in picks):
            st.session_state[picks_key] = [b for b in picks if b in options]
        branches = st.multiselect("Branches to merge", options, key=picks_key, disabled=not ready)

        clean = st.checkbox(
            "Clean up previous review worktrees",
            help="Review copies from earlier runs may contain a fix someone started. "
            "Ticking this deletes them and runs fresh.",
        )
        run = st.button("Run", type="primary", width="stretch", disabled=not ready)
    return repo, target, branches, clean, run


def read_proposals():
    # entries from proposals.jsonl, oldest first; bad lines and a missing file are skipped
    entries = []
    try:
        with open(PROPOSALS_LOG, encoding="utf-8") as fh:
            for line in fh:
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    except OSError:
        pass
    return entries


def show_stats():
    # what the AI proposed and what people did with it
    entries = read_proposals()
    if not entries:
        st.caption("No proposals logged yet.")
        return
    df = pd.DataFrame(entries).reindex(
        columns=["time", "model", "repo", "branch", "file", "decision", "changed_lines", "recommend"]
    )
    # whole numbers, blank for a reject
    df["changed_lines"] = pd.to_numeric(df["changed_lines"], errors="coerce").astype("Int64")
    counts = df["decision"].value_counts()
    tiles = st.columns(4)
    tiles[0].metric("Proposals", len(df))
    tiles[1].metric("Accepted", int(counts.get("accept", 0)))
    tiles[2].metric("Edited", int(counts.get("edit", 0)))
    tiles[3].metric("Rejected", int(counts.get("reject", 0)))

    by_model = df.assign(model=df["model"].fillna("unknown")).groupby("model")["decision"]
    table = pd.DataFrame(
        {
            "proposals": by_model.size(),
            "accepted": by_model.apply(lambda d: (d == "accept").sum()),
            "edited": by_model.apply(lambda d: (d == "edit").sum()),
            "rejected": by_model.apply(lambda d: (d == "reject").sum()),
        }
    )
    table["accepted as-is %"] = (100 * table["accepted"] / table["proposals"]).round().astype(int).astype(str) + "%"
    st.dataframe(table, width="stretch")

    st.caption("Last 20 proposals, newest first")
    last_20 = df.drop(columns="model").tail(20).iloc[::-1]
    st.dataframe(last_20, hide_index=True, width="stretch")


def main():
    st.set_page_config(page_title="Merge tool", page_icon=None, layout="wide")
    st.title("Merge tool")
    st.caption("Trial-merges branches into a target and shows which ones conflict and need a person.")

    repo, target, branches, clean, run = show_sidebar()

    show_results_now = True
    if run:
        with st.status("Merging…") as status:
            try:
                result, error = run_merge(repo, target, branches, clean, status)
            except Exception as e:
                result, error = None, f"Merge failed: {short_error(e)}"
            status.update(label="Run failed" if error else "Run finished", state="error" if error else "complete")
        if error:
            st.error(error)
            # don't show results from an earlier run next to the error
            show_results_now = False
        else:
            # keep the results so button clicks (Ask AI) don't lose them on rerun
            st.session_state["last_run"] = result
            # answers cached for an earlier run no longer match the new worktrees
            for key in [k for k in st.session_state if k.startswith(("ai:", "res:", "view:", "editing:", "text:", "qa:", "q:"))]:
                del st.session_state[key]
            st.toast("Run finished")

    last = st.session_state.get("last_run") if show_results_now else None
    first_run_hint = "Load the repo's branches in the sidebar, pick a target and branches, then press Run."
    if last:
        show_metrics(last["conflicts"])
    conflicts, overview, stats = st.tabs(["Conflicts", "Overview", "Stats"])
    with conflicts:
        if last:
            show_conflicts(last["conflicts"], last["target"])
        else:
            st.info(first_run_hint)
    with overview:
        if last:
            if last["cleaned"]:
                st.caption(f"Cleaned up: {last['cleaned']}")
            st.subheader("Branch overview")
            show_overview(last["data"])
            st.subheader("Merge results")
            show_results(last["conflicts"])
        else:
            st.info(first_run_hint)
    with stats:
        show_stats()


main()
