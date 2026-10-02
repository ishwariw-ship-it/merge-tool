import os
import subprocess

from ai_resolve import explain_conflict

REPO = os.path.expanduser("~/Documents/merge-tool-demo/repo")
FILE = "notes.txt"


def show(ref, path):
    # file content at a branch, or "" if it doesn't exist there (e.g. added on both sides)
    r = subprocess.run(["git", "-C", REPO, "show", f"{ref}:{path}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def commit_msg(ref):
    r = subprocess.run(["git", "-C", REPO, "log", "-1", "--format=%s", ref], capture_output=True, text=True)
    return r.stdout.strip()


def main():
    result = explain_conflict(
        FILE,
        show("main", FILE),
        show("branchD", FILE),
        show("branchE", FILE),
        "branchD",
        "branchE",
        commit_msg("branchD"),
        commit_msg("branchE"),
    )
    for key, value in result.items():
        print(f"--- {key}\n{value}\n")


main()
