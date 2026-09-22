#!/usr/bin/env python3
"""Create an editable one-line-per-marker decision file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def read_jsonl(path: Path) -> list[dict]:
    records = []
    with path.open("r", encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"Ошибка JSONL {path}, строка {line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise SystemExit(f"Ошибка JSONL {path}, строка {line_number}: ожидался объект")
            records.append(value)
    if not records:
        raise SystemExit(f"Файл пуст: {path}")
    return records


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Создать шаблон decisions.jsonl")
    parser.add_argument("--markers", required=True, help="Подготовленный .for-ai.jsonl")
    parser.add_argument("--out", help="Файл решений; по умолчанию рядом с markers")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    markers_path = Path(args.markers).expanduser().resolve()
    output = (
        Path(args.out).expanduser().resolve()
        if args.out
        else markers_path.with_name(markers_path.stem + ".decisions.jsonl")
    )
    if not markers_path.is_file():
        raise SystemExit(f"Файл маркеров не найден: {markers_path}")
    if output.exists() and not args.force:
        raise SystemExit(f"Файл решений уже существует: {output}; для перезаписи добавьте --force")

    templates = []
    for marker in read_jsonl(markers_path):
        identity = marker.get("identity") or {}
        templates.append(
            {
                "triage_item_id": marker.get("triage_item_id"),
                "invariant": identity.get("invariant"),
                "verdict": None,
                "confidence": None,
                "source": "",
                "control": "",
                "sink": "",
                "reachable_path": [],
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
            }
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as stream:
        for record in templates:
            stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            stream.write("\n")
    print(f"Шаблон решений: {output}")
    print(f"Записей: {len(templates)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
