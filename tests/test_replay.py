import os
import sys

# repo root on the path, so the top-level modules import when run as python3 tests/<file>.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json

from merge_tool import COMBINE_SAFE_FILES

# real conflict log from mimic-LLM-service (staging branch), copied in place by path
CONFLICTS_PATH = "/Users/ishwariwakchaure/Documents/mimic-LLM-service/merge-tool/conflicts.jsonl"


def classify(row):
    # same rule as bucket_conflict, but on the row's recorded ours/theirs text
    if os.path.basename(row["file"]) in COMBINE_SAFE_FILES:
        return "machine"
    if row["ours"] == row["theirs"]:
        return "machine"
    return "human"


def main():
    matched = 0
    mismatches = []

    with open(CONFLICTS_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)

            predicted_bucket = classify(row)
            predicted_needs_human = predicted_bucket == "human"
            actual_needs_human = row["needs_human"]

            if predicted_needs_human == actual_needs_human:
                matched += 1
            else:
                mismatches.append((row, predicted_bucket))

    print(f"{matched} matched, {len(mismatches)} mismatched")
    for row, predicted_bucket in mismatches:
        print(
            f"{row['id']} ({row['file']}): predicted={predicted_bucket}, "
            f"actual needs_human={row['needs_human']}, resolution={row['resolution']}, "
            f"reason={row['reason']}"
        )


if __name__ == "__main__":
    main()
