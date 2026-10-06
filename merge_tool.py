import argparse
import difflib
import json
import os
import shutil
import subprocess
import tempfile
import uuid

from ai_resolve import check_merged


def git(repo, *args, check=False):
    # run a git command in the repo and return the finished process
    return subprocess.run(
        ["git", "-C", repo, *args], capture_output=True, text=True, errors="replace", check=check
    )


def understand_branches(repo, target, branches):
    result = {}

    for branch in branches:
        # find the common ancestor commit, if any
        merge_base = git(repo, "merge-base", target, branch)
        base_commit = merge_base.stdout.strip()
        shares_history = merge_base.returncode == 0 and bool(base_commit)
        if not shares_history:
            base_commit = None

        if shares_history:
            output = git(repo, "diff", "--name-only", base_commit, branch).stdout
        else:
            # no common ancestor, so list the branch's files instead of diffing
            output = git(repo, "ls-tree", "-r", "--name-only", branch).stdout
        files_touched = [line for line in output.splitlines() if line]

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


def show_side(worktree, stage, path):
    # the file's content from one side of a conflict (stage 2 = ours, 3 = theirs)
    return git(worktree, "show", f":{stage}:{path}").stdout


def bucket_conflict(worktree, path):
    # decide whether a conflicted file can be resolved by machine or needs a person
    # resolution says how to fix it: "combine", "take_either", or None (needs a person)
    # numstat prints "-\t-" for a binary file (diffing the two sides: the unmerged path itself shows 0\t0)
    numstat = git(worktree, "diff", "--numstat", f":2:{path}", f":3:{path}")
    if numstat.returncode == 0 and numstat.stdout.startswith("-\t-"):
        return "human", "binary file, needs a person", None

    if os.path.basename(path) in COMBINE_SAFE_FILES:
        return "machine", "config file, safe to combine", "combine"

    if show_side(worktree, 2, path) == show_side(worktree, 3, path):
        return "machine", "both sides made the same change", "take_either"

    return "human", "both sides changed it differently, needs a person", None


class UnsafeCombine(RuntimeError):
    # the combined file looks wrong, so it is left for a person instead
    pass


def write_file(path, content):
    # temp file next to the target, then rename, so a crash never leaves a half-written file
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".merge-tool-")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    if os.path.exists(path):
        shutil.copymode(path, tmp)
    os.replace(tmp, path)


def apply_resolution(worktree, path, resolution):
    # write the resolved content for one conflicted file and stage it; raises if git can't combine
    if resolution == "take_either":
        content = show_side(worktree, 2, path)
    else:
        # combine: git's own union merge of the three stages (no stage 1 for add/add: empty file)
        with tempfile.TemporaryDirectory() as tmp:
            sides = []
            for stage in (2, 1, 3):
                side = os.path.join(tmp, str(stage))
                with open(side, "w") as f:
                    f.write(show_side(worktree, stage, path))
                sides.append(side)
            merged = git(worktree, "merge-file", "-p", "--union", *sides)
        if merged.returncode != 0:
            raise RuntimeError(f"{path}: {fatal_line(merged.stderr) or 'merge-file failed'}")
        content = merged.stdout
        # a union can share one closing fence between two code blocks
        if sum(line.startswith("```") for line in content.splitlines()) % 2:
            raise UnsafeCombine(f"{path}: code fences unbalanced after combine")

    write_file(os.path.join(worktree, path), content)
    git(worktree, "add", "--", path)


def conflict_diff(worktree, path):
    # unified diff between ours and theirs for a human-bucket conflict
    ours = show_side(worktree, 2, path)
    theirs = show_side(worktree, 3, path)
    lines = difflib.unified_diff(
        ours.splitlines(keepends=True),
        theirs.splitlines(keepends=True),
        fromfile="ours",
        tofile="theirs",
    )
    return "".join(lines)


def fatal_line(stderr):
    # the "fatal:" or "error:" line of git's message, else the last non-empty line
    lines = [l.strip() for l in stderr.splitlines() if l.strip()]
    for line in lines:
        if line.startswith(("fatal:", "error:")):
            return line
    return lines[-1] if lines else ""


def snapshot_path(worktree):
    # where the snapshot lives: inside this worktree's own git dir
    path = git(worktree, "rev-parse", "--git-path", "mergetool-snapshot").stdout.strip()
    return os.path.join(worktree, path)


def snapshot_text(worktree):
    # git status plus a hash of every file it lists, to tell edited worktrees from untouched ones
    status = git(worktree, "status", "--porcelain")
    paths = [e[3:] for e in git(worktree, "status", "--porcelain", "-z").stdout.split("\0") if e]
    lines = [status.stdout]
    for path in paths:
        if os.path.exists(os.path.join(worktree, path)):
            lines.append(f"{path}\t{git(worktree, 'hash-object', '--', path).stdout.strip()}\n")
        else:
            lines.append(f"{path}\tmissing\n")
    return "".join(lines)


def save_snapshot(worktree):
    # temp file, then rename; if it fails there is no snapshot and the worktree is treated as edited
    path = snapshot_path(worktree)
    try:
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".snapshot-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(snapshot_text(worktree))
        os.replace(tmp, path)
    except OSError:
        pass


def is_untouched(worktree):
    # True only if a snapshot exists and the worktree still matches it
    try:
        with open(snapshot_path(worktree), encoding="utf-8") as fh:
            return fh.read() == snapshot_text(worktree)
    except OSError:
        return False


def new_chain_worktree(repo, start_point, tag):
    # a fresh throwaway worktree + branch, checked out at start_point
    tmp_dir = tempfile.mkdtemp(prefix="merge_tool_")
    worktree = os.path.join(tmp_dir, "wt")
    branch_name = f"merge-tool-tmp-{uuid.uuid4().hex[:8]}-{tag}"
    git(repo, "worktree", "add", "-b", branch_name, worktree, start_point, check=True)
    return tmp_dir, worktree, branch_name


def check_merge(worktree, last_good, check_cmd):
    # a clean merge can still be broken code: compile-check the changed .py files, then run
    # the person's check command; returns the first problem found, or None
    changed = git(worktree, "diff", "--name-only", last_good, "HEAD").stdout.splitlines()
    for path in changed:
        full = os.path.join(worktree, path)
        if not path.endswith(".py") or not os.path.isfile(full):
            continue
        try:
            with open(full, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError as e:
            return f"{path}: {e.strerror}"
        problems = check_merged(path, text)
        if problems:
            return f"{path}: {problems[0]}"

    if check_cmd:
        # no .pyc files: one from an earlier merge could hide a break in this one
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            run = subprocess.run(
                check_cmd, shell=True, cwd=worktree, capture_output=True, text=True, errors="replace",
                timeout=300, env=env,
            )
        except subprocess.TimeoutExpired:
            return "check command timed out (300s)"
        if run.returncode != 0:
            why = fatal_line(run.stderr) or fatal_line(run.stdout) or f"exit {run.returncode}"
            return f"check command failed: {why}"
    return None


def sort_conflicts(repo, target, branches, branch_info, check_cmd=None):
    result = {}
    ident = ["-c", "user.name=merge-tool", "-c", "user.email=merge-tool@localhost"]

    chain_idx = 0
    tmp_dir, worktree, temp_branch = new_chain_worktree(repo, target, chain_idx)

    def fail(branch, message):
        # give up on this branch only: undo its merge, record why, go on with the next
        git(worktree, "merge", "--abort")
        result[branch] = {"failed": message}

    try:
        for branch in branches:
            if not branch_info[branch]["shares_history"]:
                result[branch] = None
                continue

            # the chain's current tip, in case this branch gets stuck and we need to resume from here
            last_good = git(worktree, "rev-parse", "HEAD").stdout.strip()

            merge = git(worktree, "merge", "--no-ff", "--no-commit", branch)
            if merge.returncode == 0:
                # clean: commit so the next branch is checked against this one too
                commit = git(worktree, *ident, "commit", "--no-edit", "-q")
                if commit.returncode != 0:
                    fail(branch, fatal_line(commit.stderr) or "commit failed")
                else:
                    # the merge stays in the chain either way; only the status changes
                    problem = check_merge(worktree, last_good, check_cmd)
                    result[branch] = {"broken": problem, "files": []} if problem else []
                continue

            conflicted = git(worktree, "diff", "--name-only", "--diff-filter=U")
            conflicted_files = [l for l in conflicted.stdout.splitlines() if l]
            if not conflicted_files:
                # the merge failed for some other reason than a conflict
                fail(branch, fatal_line(merge.stderr) or "merge failed")
                continue
            entries = []
            for path in conflicted_files:
                bucket, comment, resolution = bucket_conflict(worktree, path)
                entries.append(
                    {"file": path, "bucket": bucket, "comment": comment, "resolution": resolution}
                )

            # machine files always get fixed and staged, regardless of what else is left
            try:
                for e in entries:
                    if e["bucket"] == "machine":
                        try:
                            apply_resolution(worktree, e["file"], e["resolution"])
                        except UnsafeCombine:
                            # leave this one file for a person; the other machine files still get applied
                            e["bucket"] = "human"
                            e["comment"] = "could not combine safely, needs a person"
            except (RuntimeError, OSError) as e:
                fail(branch, str(e))
                continue

            human_left = any(e["bucket"] == "human" for e in entries)

            if not human_left:
                # everything was machine-safe: commit and keep going
                commit = git(worktree, *ident, "commit", "--no-edit", "-q")
                if commit.returncode != 0:
                    fail(branch, fatal_line(commit.stderr) or "commit failed")
                    continue
                files = [
                    {"file": e["file"], "bucket": e["bucket"], "comment": e["comment"], "applied": True}
                    for e in entries
                ]
                problem = check_merge(worktree, last_good, check_cmd)
                result[branch] = {"broken": problem, "files": files} if problem else files
                continue

            # some files still need a person: leave this worktree as-is for review, don't commit
            renamed = git(worktree, "branch", "-m", f"mergetool-review-{branch}")
            if renamed.returncode != 0:
                raise RuntimeError(fatal_line(renamed.stderr) or "could not rename the review branch")
            save_snapshot(worktree)

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
        git(repo, "worktree", "remove", "--force", worktree)
        git(repo, "branch", "-D", temp_branch)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("repo")
    parser.add_argument("target")
    parser.add_argument("branches", nargs="+")
    parser.add_argument("-o", default="branches.json")
    parser.add_argument("--check-cmd", default=None, help="shell command run in the merged copy after each clean merge")
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
    conflicts = sort_conflicts(args.repo, args.target, args.branches, data, args.check_cmd)
    for branch, files in conflicts.items():
        if files is None:
            print(f"{branch}: skipped (no shared history)")
        elif isinstance(files, dict) and "broken" in files:
            print(f"{branch}: merged but broken ({files['broken']})")
        elif isinstance(files, dict):
            print(f"{branch}: failed ({files['failed']})")
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
