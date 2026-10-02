import os
import subprocess

import pandas as pd
import streamlit as st

from merge_tool import sort_conflicts, understand_branches

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
    # delete the old worktrees and branches, return a list of what was removed
    removed = []
    kept = set()
    for review, path in leftovers.items():
        if not path:
            continue
        # any output means the person edited files there: keep worktree and branch
        status = git(path, "status", "--porcelain")
        if status.returncode != 0:
            st.warning(f"Could not check {review}, not removed: {status.stderr.strip()}")
            kept.add(review)
        elif status.stdout.strip():
            st.warning(f"{review} has unsaved edits, not removed — finish or discard them first.")
            kept.add(review)
        else:
            # no --force: git itself refuses if the worktree is somehow dirty
            result = git(repo, "worktree", "remove", path)
            if result.returncode != 0:
                st.warning(f"Could not remove {review}: {result.stderr.strip()}")
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
    return removed


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
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)


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
    table = pd.DataFrame(rows).style.map(color_status, subset=["status"])
    st.dataframe(table, hide_index=True, use_container_width=True)


def show_branch_details(conflicts):
    for branch, files in conflicts.items():
        status = status_of(files)
        if status not in ("auto-fixed", "needs human"):
            continue
        with st.expander(f"{branch} - {status}"):
            machine = [f for f in files if f["bucket"] == "machine"]
            human = [f for f in files if f["bucket"] == "human"]
            for f in machine:
                st.write(f"Auto-fixed `{f['file']}`: {f['comment']}")
            if human:
                st.write(f"Review worktree kept at: `{human[0]['review_path']}`")
            for f in human:
                st.write(f"Needs human: `{f['file']}` ({f['comment']})")
                # the kept worktree holds the file with conflict markers
                path = os.path.join(f["review_path"], f["file"])
                with open(path, errors="replace") as fh:
                    st.code(fh.read())


def main():
    st.set_page_config(page_title="Merge tool", layout="wide")
    st.title("Merge tool")

    with st.sidebar:
        repo = st.text_input("Repo path")
        target = st.text_input("Target branch", value="main")
        names = st.text_area("Branch names (one per line)")
        clean = st.checkbox(
            "Clean up previous review worktrees",
            help="Review copies from earlier runs may contain a fix someone started. "
            "Ticking this deletes them and runs fresh.",
        )
        run = st.button("Run")

    if not run:
        st.info("Fill in the sidebar and press Run.")
        return

    repo = os.path.expanduser(repo.strip())
    target = target.strip()
    branches = [line.strip() for line in names.splitlines() if line.strip()]

    error = check_inputs(repo, target, branches)
    if error:
        st.error(error)
        return

    leftovers = find_leftovers(repo, branches)
    if leftovers and not clean:
        st.error(
            f"A review copy from an earlier run still exists for: {', '.join(leftovers)}. "
            "It may contain a fix someone started. Ticking 'Clean up previous review "
            "worktrees' will delete it and run fresh."
        )
        return
    if clean:
        removed = remove_leftovers(repo, leftovers)
        st.write("Cleaned up: " + (", ".join(removed) or "nothing to clean"))

    with st.spinner("Inspecting and trial-merging branches..."):
        data = understand_branches(repo, target, branches)
        conflicts = sort_conflicts(repo, target, branches, data)

    statuses = [status_of(files) for files in conflicts.values()]
    st.write(
        f"**{statuses.count('clean')}** clean, "
        f"**{statuses.count('auto-fixed')}** auto-fixed, "
        f"**{statuses.count('needs human')}** needs human, "
        f"**{statuses.count('skipped')}** skipped"
    )

    st.subheader("Branch overview")
    show_overview(data)
    st.subheader("Merge results")
    show_results(conflicts)
    show_branch_details(conflicts)


main()
