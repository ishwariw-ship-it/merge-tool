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


def check_inputs(repo, target, branches):
    # return an error message, or None if the repo and every ref look fine
    if not os.path.isdir(repo):
        return f"Repo path does not exist: {repo}"
    inside = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
    )
    if inside.returncode != 0:
        return f"Not a git repository: {repo}"
    if not branches:
        return "Enter at least one branch name."
    for ref in [target] + branches:
        found = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            capture_output=True,
            text=True,
        )
        if found.returncode != 0:
            return f"Branch does not exist: {ref}"
    return None


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
    rows = [
        {
            "branch": branch,
            "shares history": info["shares_history"],
            "files touched": len(info["files_touched"]),
            "overlaps with": ", ".join(info["overlap"]) or "-",
        }
        for branch, info in data.items()
    ]
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
            for f in files:
                if f["bucket"] == "machine":
                    st.write(f"Auto-fixed `{f['file']}`: {f['comment']}")
            humans = [f for f in files if f["bucket"] == "human"]
            if humans:
                st.write(f"Review worktree kept at: `{humans[0]['review_path']}`")
            for f in humans:
                st.write(f"Needs human: `{f['file']}` ({f['comment']})")
                # the kept worktree holds the file with conflict markers
                path = os.path.join(f["review_path"], f["file"])
                with open(path, errors="replace") as fh:
                    st.code(fh.read())


def clean_review(repo, branches, clean):
    # find leftover mergetool-review-<branch> worktrees/branches; remove them if clean is on
    # returns (error, removed): error if leftovers exist and clean is off
    listing = subprocess.run(
        ["git", "-C", repo, "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
    ).stdout
    # map branch name -> worktree path
    worktrees = {}
    path = None
    for line in listing.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/"):
            worktrees[line[len("branch refs/heads/"):]] = path

    found = []
    for branch in branches:
        review = f"mergetool-review-{branch}"
        exists = subprocess.run(
            ["git", "-C", repo, "branch", "--list", review], capture_output=True, text=True
        ).stdout.strip()
        if exists or review in worktrees:
            found.append(review)

    if found and not clean:
        names = ", ".join(found)
        return f"Old review branches exist ({names}). Tick 'Clean up previous review worktrees'.", []
    if not clean:
        return None, []

    removed = []
    for review in found:
        if review in worktrees:
            subprocess.run(
                ["git", "-C", repo, "worktree", "remove", "--force", worktrees[review]],
                capture_output=True,
                text=True,
            )
            removed.append(f"worktree {worktrees[review]}")
    # drop entries for worktrees already deleted on disk, so their branches can be deleted
    subprocess.run(["git", "-C", repo, "worktree", "prune"], capture_output=True, text=True)
    for review in found:
        subprocess.run(["git", "-C", repo, "branch", "-D", review], capture_output=True, text=True)
        removed.append(f"branch {review}")
    return None, removed


def main():
    st.set_page_config(page_title="Merge tool", layout="wide")
    st.title("Merge tool")

    with st.sidebar:
        repo = st.text_input("Repo path")
        target = st.text_input("Target branch", value="main")
        names = st.text_area("Branch names (one per line)")
        clean = st.checkbox("Clean up previous review worktrees")
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

    error, removed = clean_review(repo, branches, clean)
    if error:
        st.error(error)
        return
    if clean:
        st.write("Cleaned up: " + (", ".join(removed) if removed else "nothing to clean"))

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
