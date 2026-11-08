"""导入带运行/输出指纹的人工评分；不把未评分算作零分或通过。"""

import argparse
import json
from pathlib import Path

from .graders import RUBRIC
from .runner import dump


def review(folder, ratings):
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    rows = [
        json.loads(line)
        for line in (folder / "results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    index = {(row["id"], row["repeat"]): row for row in rows}
    entries = json.loads(Path(ratings).read_text(encoding="utf-8"))
    seen = set()
    accepted = []
    for entry in entries:
        key = (entry["id"], entry["repeat"])
        if key in seen or key not in index:
            raise ValueError("duplicate or unknown review target")
        seen.add(key)
        row = index[key]
        if (
            entry["run_id"] != manifest["run_id"]
            or entry["output_sha256"] != row["output_sha256"]
            or row["mode"] != "live"
            or row.get("quality", {}).get("status") != "pending_human_review"
        ):
            raise ValueError("review does not match a real model output")
        scores = entry["scores"]
        if set(scores) != set(RUBRIC) or any(
            type(value) is not int or not 0 <= value <= 3 for value in scores.values()
        ):
            raise ValueError("every rubric dimension requires an integer score from 0 to 3")
        if not entry.get("evidence", "").strip() or not entry.get("reviewer", "").strip():
            raise ValueError("reviewer and supporting evidence are required")
        accepted.append(entry)
    targets = {
        key
        for key, row in index.items()
        if row["mode"] == "live" and row.get("quality", {}).get("status") == "pending_human_review"
    }
    result = {
        "run_id": manifest["run_id"],
        "reviewed": len(accepted),
        "total_review_targets": len(targets),
        "remaining": len(targets - seen),
        "status": "complete" if targets and targets == seen else "partial",
        "dimension_means": {
            dimension: sum(e["scores"][dimension] for e in accepted) / len(accepted)
            for dimension in RUBRIC
        }
        if accepted
        else None,
        "reviews": accepted,
        "note": "Human rubric scores are separate from program hard checks.",
    }
    dump(folder / "human-review.json", result)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("report_directory", type=Path)
    parser.add_argument("ratings", type=Path)
    args = parser.parse_args()
    print(json.dumps(review(args.report_directory, args.ratings), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
