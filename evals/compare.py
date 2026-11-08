"""相同固定输入的版本对比；缺失样例不悄悄排除。"""

import argparse
import json
from pathlib import Path

from .runner import dump


def compare(before, after):
    def load(folder):
        folder = Path(folder)
        manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
        rows = [
            json.loads(line)
            for line in (folder / "results.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        return manifest, {(r["id"], r["repeat"]): r for r in rows}

    old, a = load(before)
    new, b = load(after)
    for key in ("dataset_sha256", "fixtures_sha256", "as_of", "mode"):
        if old[key] != new[key]:
            raise ValueError("incomparable inputs or execution mode: " + key)
    if a.keys() != b.keys():
        raise ValueError("case/repeat sets differ")
    differences = []
    for key, left in a.items():
        right = b[key]
        if left["mode"] != right["mode"]:
            raise ValueError("per-case execution modes differ")
        differences.append(
            {
                "id": key[0],
                "repeat": key[1],
                "before": left["status"],
                "after": right["status"],
                "regression": left["status"] == "passed" and right["status"] != "passed",
                "elapsed_delta": right.get("metrics", {}).get("elapsed_seconds", 0)
                - left.get("metrics", {}).get("elapsed_seconds", 0),
            }
        )
    return {
        "before": old["run_id"],
        "after": new["run_id"],
        "regressions": sum(d["regression"] for d in differences),
        "cases": differences,
        "quality_comparison": "not_automatically_inferred_from_hard_checks",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.before, args.after)
    dump(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
