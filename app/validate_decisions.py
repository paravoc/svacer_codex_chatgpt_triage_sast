#!/usr/bin/env python3
"""Validate that Codex produced one complete decision per extracted marker."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


STATUSES = {"Confirmed", "False Positive", "Won't fix", "Unclear"}
CONFIRMED_SEVERITIES = {"Critical", "Major", "Minor"}
CONFIRMED_ACTIONS = {"Fix required", "Fix submitted", "Ignore"}
HEADINGS = {
    "Confirmed": "CONFIRMED",
    "False Positive": "FALSE POSITIVE",
    "Won't fix": "WONT FIX",
    "Unclear": "UNCLEAR",
}


def load(path: Path) -> list[dict]:
    result = []
    with path.open("r", encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Некорректный JSONL {path}, строка {line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise SystemExit(f"В {path}, строка {line_number}, ожидался JSON-объект")
            result.append(value)
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Проверить решения по JSONL-маркерам")
    parser.add_argument("--markers", required=True)
    parser.add_argument("--decisions", required=True)
    args = parser.parse_args()

    markers = load(Path(args.markers).expanduser().resolve())
    decisions = load(Path(args.decisions).expanduser().resolve())
    expected = {str(item.get("triage_item_id")): item for item in markers}
    errors: list[str] = []
    seen: Counter[str] = Counter()

    for decision in decisions:
        item_id = str(decision.get("triage_item_id") or "")
        seen[item_id] += 1
        marker = expected.get(item_id)
        if marker is None:
            errors.append(f"неизвестный triage_item_id: {item_id}")
            continue
        if decision.get("invariant") != (marker.get("identity") or {}).get("invariant"):
            errors.append(f"{item_id}: invariant не совпадает")
        verdict = decision.get("verdict")
        if verdict not in STATUSES:
            errors.append(f"{item_id}: недопустимый verdict {verdict!r}")
            continue
        if decision.get("confidence") not in {"high", "medium", "low"}:
            errors.append(f"{item_id}: confidence должен быть high, medium или low")
        for field in ("source", "control", "sink"):
            if not isinstance(decision.get(field), str) or not decision[field].strip():
                errors.append(f"{item_id}: поле {field} пустое")
        if not isinstance(decision.get("reachable_path"), list):
            errors.append(f"{item_id}: reachable_path должен быть массивом")
        comment = decision.get("comment")
        first_line = next((line.strip() for line in str(comment or "").splitlines() if line.strip()), "")
        if first_line != HEADINGS[verdict]:
            errors.append(f"{item_id}: комментарий должен начинаться с {HEADINGS[verdict]}")
        if verdict == "Confirmed":
            if decision.get("severity") not in CONFIRMED_SEVERITIES:
                errors.append(f"{item_id}: для Confirmed нужна severity")
            if decision.get("action") not in CONFIRMED_ACTIONS:
                errors.append(f"{item_id}: для Confirmed нужен action")
        else:
            if "severity" in decision or "action" in decision:
                errors.append(f"{item_id}: для {verdict} severity/action должны отсутствовать")

    for item_id in expected:
        if seen[item_id] == 0:
            errors.append(f"отсутствует решение: {item_id}")
        elif seen[item_id] > 1:
            errors.append(f"дубликат решения: {item_id}")

    print(f"Маркеров: {len(markers)}")
    print(f"Решений: {len(decisions)}")
    if errors:
        print(f"Ошибок: {len(errors)}")
        for error in errors[:100]:
            print(f"- {error}")
        return 2
    counts = Counter(item["verdict"] for item in decisions)
    print("Проверка пройдена")
    for verdict, count in sorted(counts.items()):
        print(f"{verdict}: {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
