#!/usr/bin/env python3
"""Create a resumable decision file from a Svacer MCP marker inventory."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from triage_queue import load_inventory, markers_for_triage, marker_review_status


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    inventory = Path(args.inventory).expanduser().resolve()
    output = Path(args.out).expanduser().resolve()
    if not inventory.is_file():
        raise SystemExit(f"Инвентарь не найден: {inventory}")
    if output.exists() and not args.force:
        raise SystemExit(f"Файл уже существует: {output}")

    all_markers = load_inventory(inventory)
    markers = markers_for_triage(all_markers)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w" if args.force else "x", encoding="utf-8", newline="\n") as stream:
        for marker in markers:
            record = {
                "schema_version": 2,
                "marker_id": marker["id"],
                "warnClass": marker.get("warnClass"),
                "file": marker.get("file"),
                "line": marker.get("line"),
                "verdict": None,
                "confidence": None,
                "entrypoint": "",
                "source": "",
                "control": "",
                "sink": "",
                "build_reachability": "",
                "product_reachability": "",
                "reachable_path": [],
                "impact": "",
                "boundary": {
                    "product_surface": "unknown",
                    "source_trust": "unknown",
                    "boundary_crossed": None,
                    "policy_basis": "unknown",
                },
                "evidence": [],
                "counterevidence": [],
                "proof_gaps": [],
                "comment": "",
                "verification": {
                    "status": "not_required",
                    "verifier_id": None,
                    "reason": "",
                    "evidence": [],
                    "rechecked_paths": [],
                    "verified_at": None,
                },
            }
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")

    print(f"Шаблон: {output}")
    print(f"ГОСТ-маркеров всего: {len(all_markers)}")
    print(f"Уже размечено в Svacer: {sum(marker_review_status(m) != 'Undecided' for m in all_markers)}")
    print(f"Доступно для выбора и перепроверки: {len(markers)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
