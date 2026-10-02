import json
import subprocess

from ai_resolve import explain_conflict
from test_replay import CONFLICTS_PATH, classify

REPO = "/Users/ishwariwakchaure/Documents/mimic-LLM-service"
RESULTS_PATH = "ai_replay_results.jsonl"


def run_git(*args):
    # run git in the repo, return (stdout, None) or (None, short error message)
    r = subprocess.run(["git", "-C", REPO, *args], capture_output=True, text=True)
    if r.returncode != 0:
        lines = r.stderr.strip().splitlines()
        return None, lines[0] if lines else "git failed"
    return r.stdout, None


def branches_of(row):
    # the row's "merge" field looks like "staging <- feature/x" (ours <- theirs)
    ours, sep, theirs = row.get("merge", "").partition(" <- ")
    if not sep:
        return "unknown", "unknown"
    return ours.strip() or "unknown", theirs.strip() or "unknown"


def normalize(text):
    # compare ignoring trailing whitespace on each line
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def load_row_inputs(row):
    # read the real file contents and commit messages, return (inputs, error)
    path = row["file"]
    inputs = {}
    for key, ref in (
        ("ours", row["ours_sha"]),
        ("theirs", row["theirs_sha"]),
        # staging may have later edits, so this is a close approximation of the human result
        ("actual", "staging"),
    ):
        inputs[key], error = run_git("show", f"{ref}:{path}")
        if error:
            return None, f"git show {ref}:{path}: {error}"
    for key, sha in (("our_msg", row["ours_sha"]), ("their_msg", row["theirs_sha"])):
        out, error = run_git("log", "-1", "--format=%s", sha)
        if error:
            return None, f"git log {sha}: {error}"
        inputs[key] = out.strip() or "unknown"
    return inputs, None


def main():
    ran = 0
    ok_count = 0
    same_count = 0

    # results file is regenerated on every run
    with open(CONFLICTS_PATH) as f, open(RESULTS_PATH, "w") as out:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)

            # the rules already handle machine conflicts, only try the human ones
            if classify(row) == "machine":
                continue

            print(f"{row['id']} ({row['file']}) ...")
            inputs, error = load_row_inputs(row)
            if error:
                print(f"  skipped: {error}")
                result = {"ok": False, "raw_text": error}
                inputs = {"actual": None}
            else:
                our_branch, their_branch = branches_of(row)
                # all rows are add/add, so there is no base file
                result = explain_conflict(
                    row["file"],
                    "",
                    inputs["ours"],
                    inputs["theirs"],
                    our_branch,
                    their_branch,
                    inputs["our_msg"],
                    inputs["their_msg"],
                )
            ran += 1
            ok_count += bool(result["ok"])

            same = bool(
                result["ok"]
                and inputs["actual"] is not None
                and isinstance(result["merged"], str)
                and normalize(result["merged"]) == normalize(inputs["actual"])
            )
            same_count += same

            record = {
                "id": row["id"],
                "file": row["file"],
                "explanation": result.get("explanation"),
                "risk_ours": result.get("risk_ours"),
                "risk_theirs": result.get("risk_theirs"),
                "merged": result.get("merged"),
                "confidence": result.get("confidence"),
                "ok": result["ok"],
                "raw_text": result.get("raw_text"),
                "actual_resolution": row.get("resolution"),
                "actual_reason": row.get("reason"),
                "actual_merged": inputs["actual"],
                "same_as_actual": same,
            }
            out.write(json.dumps(record) + "\n")

    print(
        f"{ran} ran, {ok_count} returned ok, {same_count} same as actual. "
        f"Results in {RESULTS_PATH}"
    )


if __name__ == "__main__":
    main()
