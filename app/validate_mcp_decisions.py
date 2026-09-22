#!/usr/bin/env python3
"""Validate completeness and Svacer field rules for MCP triage decisions."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from triage_queue import (
    load_inventory,
    markers_for_triage,
    marker_review_status,
    validate_worker_result,
    verification_status,
)


def read_jsonl(path: Path) -> list[dict]:
    result = []
    with path.open("r", encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Ошибка JSONL, строка {line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise SystemExit(f"Строка {line_number}: ожидался JSON-объект")
            result.append(value)
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--inventory", required=True)
    parser.add_argument("--decisions", required=True)
    args = parser.parse_args()

    all_markers = load_inventory(Path(args.inventory).expanduser().resolve())
    all_inventory = {str(marker["id"]): marker for marker in all_markers}
    inventory = {str(marker["id"]): marker for marker in markers_for_triage(all_markers)}
    decisions = read_jsonl(Path(args.decisions).expanduser().resolve())
    seen: Counter[str] = Counter()
    errors: list[str] = []

    for decision in decisions:
        marker_id = str(decision.get("marker_id") or "")
        seen[marker_id] += 1
        if marker_id not in all_inventory:
            errors.append(f"неизвестный marker_id: {marker_id}")
            continue
        if marker_id not in inventory:
            continue
        marker = dict(inventory[marker_id])
        if decision.get("schema_version") is not None:
            marker["schema_version"] = decision.get("schema_version")
        override = decision.get("manual_verdict_override")
        errors.extend(validate_worker_result(
            decision, marker,
            allow_manual_verdict_override=isinstance(override, dict)
            and override.get("verdict") == decision.get("verdict"),
        ))
        if decision.get("verdict") == "Confirmed" and verification_status(decision) != "verified":
            errors.append(f"{marker_id}: Confirmed не прошёл независимую проверку")

    for marker_id in inventory:
        if seen[marker_id] == 0:
            errors.append(f"отсутствует решение: {marker_id}")
        elif seen[marker_id] > 1:
            errors.append(f"дубликат решения: {marker_id}")

    target_decisions = [item for item in decisions if str(item.get("marker_id") or "") in inventory]
    print(f"ГОСТ-маркеров всего: {len(all_inventory)}")
    print(f"Уже размечено в Svacer: {sum(marker_review_status(m) != 'Undecided' for m in all_markers)}")
    print(f"Доступно для анализа и перепроверки: {len(inventory)}")
    print(f"Локальных решений: {len(target_decisions)}")
    if errors:
        print(f"Ошибок: {len(errors)}")
        for error in errors[:200]:
            print(f"- {error}")
        return 2

    print("Проверка пройдена")
    for verdict, count in sorted(Counter(item["verdict"] for item in target_decisions).items()):
        print(f"{verdict}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
