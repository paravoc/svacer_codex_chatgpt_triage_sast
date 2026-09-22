#!/usr/bin/env python3
"""Create an Excel-friendly review table. This does not import anything to Svacer."""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8-sig") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--decisions", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    decisions = read_jsonl(Path(args.decisions).expanduser().resolve())
    output = Path(args.out).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    columns = [
        "marker_id", "warnClass", "file", "line", "verdict", "severity",
        "action", "confidence", "comment", "entrypoint", "source", "control", "sink",
        "build_reachability", "product_reachability", "reachable_path", "impact",
        "evidence", "counterevidence", "proof_gaps", "verification_status",
        "verification_verifier", "verification_reason", "verification_evidence",
    ]
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for item in decisions:
            row = dict(item)
            for name in ("reachable_path", "evidence", "counterevidence", "proof_gaps"):
                row[name] = "\n".join(str(value) for value in item.get(name, []))
            verification = item.get("verification") if isinstance(item.get("verification"), dict) else {}
            row["verification_status"] = verification.get("status", "")
            row["verification_verifier"] = verification.get("verifier_id", "")
            row["verification_reason"] = verification.get("reason", "")
            row["verification_evidence"] = "\n".join(
                str(value) for value in verification.get("evidence", [])
            )
            writer.writerow(row)
    print(f"CSV для ручной проверки: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
