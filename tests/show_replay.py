import difflib
import json
import os

RESULTS_PATH = "ai_replay_results.jsonl"
OUT_DIR = "replay_review"


def fence(text):
    # a code block that can't be broken by backticks inside the text
    ticks = "```"
    while ticks in text:
        ticks += "`"
    return f"{ticks}\n{text}\n{ticks}"


def render(row):
    model = row.get("merged") or ""
    actual = row.get("actual_merged") or ""
    # trailing newline so the last diff line doesn't run into the next
    diff = "".join(
        difflib.unified_diff(
            (model + "\n").splitlines(keepends=True),
            (actual + "\n").splitlines(keepends=True),
            fromfile="model",
            tofile="actual (staging)",
        )
    )
    checks = row.get("checks") or []
    port = row.get("port_from_other") or []
    parts = [
        f"# {row['id']} - {row['file']}",
        f"**Confidence:** {row.get('confidence')}  \n**ok:** {row.get('ok')}",
        "**Checks:** " + ("; ".join(checks) if checks else "passed"),
        f"## Explanation\n{row.get('explanation')}",
        f"## Risk if we keep ours\n{row.get('risk_ours')}",
        f"## Risk if we keep theirs\n{row.get('risk_theirs')}",
        f"## MODEL MERGED\n{fence(model)}",
        f"## ACTUAL (staging)\n{fence(actual)}",
        f"## Diff (model vs actual)\n{fence((diff or '(no differences)').rstrip())}",
    ]
    if row.get("recommend"):
        parts.insert(6, f"## Recommended base\n{row['recommend']}")
    if port:
        parts.insert(7 if row.get("recommend") else 6, "## Worth porting from the other side\n" + "\n".join(f"- {item}" for item in port))
    if not row.get("ok"):
        parts.insert(2, f"## Error\n{row.get('raw_text')}")
    return "\n\n".join(parts) + "\n"


def main():
    if not os.path.exists(RESULTS_PATH):
        print(f"{RESULTS_PATH} not found, run test_ai_replay.py first.")
        return
    os.makedirs(OUT_DIR, exist_ok=True)

    count = 0
    with open(RESULTS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                # named by id, since the same file can appear in two rows
                path = os.path.join(OUT_DIR, f"{row['id']}.md")
                with open(path, "w") as out:
                    out.write(render(row))
                count += 1
            except (ValueError, KeyError, OSError) as e:
                print(f"skipped a row: {type(e).__name__}")
    print(f"Wrote {count} files to {OUT_DIR}/")


if __name__ == "__main__":
    main()
