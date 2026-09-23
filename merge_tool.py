import argparse
import difflib
import json
import os
import shutil
import subprocess
import tempfile


def understand_branches(repo, target, branches):
    result = {}

    for branch in branches:
        # find the common ancestor commit, if any
        merge_base = subprocess.run(
            ["git", "-C", repo, "merge-base", target, branch],
            capture_output=True,
            text=True,
        )
        base_commit = merge_base.stdout.strip()
        shares_history = merge_base.returncode == 0 and bool(base_commit)
        if not shares_history:
            base_commit = None

        if shares_history:
            diff = subprocess.run(
                ["git", "-C", repo, "diff", "--name-only", base_commit, branch],
                capture_output=True,
                text=True,
            )
            files_touched = [line for line in diff.stdout.splitlines() if line]
        else:
            # no common ancestor, so list the branch's files instead of diffing
            ls_tree = subprocess.run(
                ["git", "-C", repo, "ls-tree", "-r", "--name-only", branch],
                capture_output=True,
                text=True,
            )
            files_touched = [line for line in ls_tree.stdout.splitlines() if line]

        result[branch] = {
            "base_commit": base_commit,
            "shares_history": shares_history,
            "files_touched": files_touched,
        }

    # compute overlap of files_touched between each pair of branches
    for branch, info in result.items():
        overlap = {}
        branch_files = set(info["files_touched"])
        for other_branch, other_info in result.items():
            if other_branch == branch:
                continue
            shared = branch_files & set(other_info["files_touched"])
            if shared:
                overlap[other_branch] = sorted(shared)
        info["overlap"] = overlap

    return result


# files where combining both sides is always safe
COMBINE_SAFE_FILES = {".env.example", ".gitignore", "README.md"}


def bucket_conflict(worktree, path):
    # decide whether a conflicted file can be resolved by machine or needs a person
    # resolution says how to fix it: "combine", "take_either", or None (needs a person)
    if os.path.basename(path) in COMBINE_SAFE_FILES:
        return "machine", "config file, safe to combine", "combine"

    ours = subprocess.run(
        ["git", "-C", worktree, "show", f":2:{path}"], capture_output=True, text=True
    )
    theirs = subprocess.run(
        ["git", "-C", worktree, "show", f":3:{path}"], capture_output=True, text=True
    )
    if ours.stdout == theirs.stdout:
        return "machine", "both sides made the same change", "take_either"

    return "human", "both sides changed it differently, needs a person", None


def apply_resolution(worktree, path, resolution):
    # write the resolved content for one conflicted file and stage it
    ours = subprocess.run(
        ["git", "-C", worktree, "show", f":2:{path}"], capture_output=True, text=True
    ).stdout

    if resolution == "take_either":
        content = ours
    else:
        # combine: ours' lines first, then theirs' lines that aren't already present
        theirs = subprocess.run(
            ["git", "-C", worktree, "show", f":3:{path}"], capture_output=True, text=True
        ).stdout
        combined_lines = ours.splitlines()
        for line in theirs.splitlines():
            if line not in combined_lines:
                combined_lines.append(line)
        content = "\n".join(combined_lines)
        if combined_lines:
            content += "\n"

    with open(os.path.join(worktree, path), "w") as f:
        f.write(content)

    subprocess.run(["git", "-C", worktree, "add", path], capture_output=True, text=True)


def conflict_diff(worktree, path):
    # unified diff between ours (:2:) and theirs (:3:) for a human-bucket conflict
    ours = subprocess.run(
        ["git", "-C", worktree, "show", f":2:{path}"], capture_output=True, text=True
    ).stdout
    theirs = subprocess.run(
        ["git", "-C", worktree, "show", f":3:{path}"], capture_output=True, text=True
    ).stdout
    lines = difflib.unified_diff(
        ours.splitlines(keepends=True),
        theirs.splitlines(keepends=True),
        fromfile="ours",
        tofile="theirs",
    )
    return "".join(lines)


def new_chain_worktree(repo, start_point, tag):
    # a fresh throwaway worktree + branch, checked out at start_point
    tmp_dir = tempfile.mkdtemp(prefix="merge_tool_")
    worktree = os.path.join(tmp_dir, "wt")
    branch_name = f"merge-tool-tmp-{os.getpid()}-{tag}"
    subprocess.run(
        ["git", "-C", repo, "worktree", "add", "-b", branch_name, worktree, start_point],
        capture_output=True,
        text=True,
        check=True,
    )
    return tmp_dir, worktree, branch_name


def sort_conflicts(repo, target, branches, branch_info):
    result = {}
    ident = ["-c", "user.name=merge-tool", "-c", "user.email=merge-tool@localhost"]

    chain_idx = 0
    tmp_dir, worktree, temp_branch = new_chain_worktree(repo, target, chain_idx)

    try:
        for branch in branches:
            if not branch_info[branch]["shares_history"]:
                result[branch] = None
                continue

            # the chain's current tip, in case this branch gets stuck and we need to resume from here
            last_good = subprocess.run(
                ["git", "-C", worktree, "rev-parse", "HEAD"], capture_output=True, text=True
            ).stdout.strip()

            merge = subprocess.run(
                ["git", "-C", worktree, "merge", "--no-ff", "--no-commit", branch],
                capture_output=True,
                text=True,
            )
            if merge.returncode == 0:
                # clean: commit so the next branch is checked against this one too
                subprocess.run(
                    ["git", "-C", worktree, *ident, "commit", "--no-edit", "-q"],
                    capture_output=True,
                    text=True,
                )
                result[branch] = []
                continue

            conflicted = subprocess.run(
                ["git", "-C", worktree, "diff", "--name-only", "--diff-filter=U"],
                capture_output=True,
                text=True,
            )
            conflicted_files = [l for l in conflicted.stdout.splitlines() if l]
            entries = []
            for path in conflicted_files:
                bucket, comment, resolution = bucket_conflict(worktree, path)
                entries.append(
                    {"file": path, "bucket": bucket, "comment": comment, "resolution": resolution}
                )

            # machine files always get fixed and staged, regardless of what else is left
            for e in entries:
                if e["bucket"] == "machine":
                    apply_resolution(worktree, e["file"], e["resolution"])

            human_left = any(e["bucket"] == "human" for e in entries)

            if not human_left:
                # everything was machine-safe: commit and keep going
                subprocess.run(
                    ["git", "-C", worktree, *ident, "commit", "--no-edit", "-q"],
                    capture_output=True,
                    text=True,
                )
                result[branch] = [
                    {"file": e["file"], "bucket": e["bucket"], "comment": e["comment"], "applied": True}
                    for e in entries
                ]
                continue

            # some files still need a person: leave this worktree as-is for review, don't commit
            review_branch = f"mergetool-review-{branch}"
            subprocess.run(
                ["git", "-C", worktree, "branch", "-m", review_branch],
                capture_output=True,
                text=True,
            )

            report = []
            for e in entries:
                item = {
                    "file": e["file"],
                    "bucket": e["bucket"],
                    "comment": e["comment"],
                    "applied": e["bucket"] == "machine",
                    "review_path": worktree,
                }
                if e["bucket"] == "human":
                    item["diff"] = conflict_diff(worktree, e["file"])
                report.append(item)
            result[branch] = report

            # this worktree is now kept for review - start a fresh one from the last good
            # commit so a stuck branch doesn't block the rest of the list
            chain_idx += 1
            tmp_dir, worktree, temp_branch = new_chain_worktree(repo, last_good, chain_idx)
    finally:
        # remove only the current (non-kept) worktree and its branch
        subprocess.run(
            ["git", "-C", repo, "worktree", "remove", "--force", worktree],
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "-C", repo, "branch", "-D", temp_branch],
            capture_output=True,
            text=True,
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repo")
    parser.add_argument("target")
    parser.add_argument("branches", nargs="+")
    parser.add_argument("-o", default="branches.json")
    args = parser.parse_args()

    data = understand_branches(args.repo, args.target, args.branches)

    for branch, info in data.items():
        overlaps = list(info["overlap"].keys())
        print(
            f"{branch}: shares_history={info['shares_history']}, "
            f"{len(info['files_touched'])} files touched, overlaps with {overlaps}"
        )

    with open(args.o, "w") as f:
        json.dump(data, f, indent=2)

    # stage 2: try merging each branch and report conflicts
    conflicts = sort_conflicts(args.repo, args.target, args.branches, data)
    for branch, files in conflicts.items():
        if files is None:
            print(f"{branch}: skipped (no shared history)")
        elif files:
            for f in files:
                print(
                    f"{branch}: {f['file']} -> {f['bucket']}, applied={f['applied']} "
                    f"({f['comment']})"
                )
            review_path = files[0].get("review_path")
            if review_path:
                print(f"{branch}: needs review, worktree kept at {review_path}")
        else:
            print(f"{branch}: clean")


if __name__ == "__main__":
    main()
