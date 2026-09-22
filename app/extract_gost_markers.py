#!/usr/bin/env python3
"""Extract the exact Svacer marker list from a full SARIF snapshot.

The CSV exported from the active GOST filter is the selection list.  SARIF is
the source of marker identities, locations and complete code-flow traces.
The output is compact JSONL: one self-contained record per CSV row, in the same
order.  Duplicate-looking CSV rows are intentionally preserved.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict, deque
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import unquote, urlparse


SCHEMA_VERSION = "svacer-gost-marker/v1"
REQUIRED_CSV_COLUMNS = ("Severity", "Checker", "File", "Line")
REVIEW_PROPERTY_NAMES = (
    "status",
    "severity",
    "action",
    "reviewed_by",
    "review_ts",
    "comments",
)
KNOWN_SCAN_PREFIXES = ("/src/src/", "/app/")


class ExtractError(RuntimeError):
    """An input problem that should be shown to the operator."""


def fail(message: str) -> None:
    raise ExtractError(message)


def text(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("text")
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def compact(value: Any) -> Any:
    """Recursively remove empty optional values without changing false/zero."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = compact(item)
            if normalized is not None and normalized != "" and normalized != [] and normalized != {}:
                result[key] = normalized
        return result
    if isinstance(value, list):
        result_list = [compact(item) for item in value]
        return [item for item in result_list if item is not None and item != "" and item != [] and item != {}]
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_from_paste(value: str) -> Path:
    """Accept a plain path, quoted path, Markdown link or local file URI."""
    pasted = value.strip()
    markdown = re.fullmatch(r"\[[^]]*\]\((.+)\)", pasted)
    if markdown:
        pasted = markdown.group(1).strip()
    if pasted.startswith("<") and pasted.endswith(">"):
        pasted = pasted[1:-1].strip()
    pasted = pasted.strip('"\'')
    lowered = pasted.lower()
    if lowered.startswith(("http://", "https://")):
        fail(
            "HTTP-ссылка не поддерживается: сначала скачайте файл из Svacer и "
            "вставьте локальный путь"
        )
    if lowered.startswith("file:"):
        parsed = urlparse(pasted)
        local = unquote(parsed.path)
        if parsed.netloc:
            local = f"//{parsed.netloc}{local}"
        elif re.match(r"^/[A-Za-z]:/", local):
            local = local[1:]
        pasted = local
    if not pasted:
        fail("Путь не указан")
    return Path(pasted).expanduser().resolve()


def prompt_path(label: str) -> Path:
    try:
        return path_from_paste(input(label))
    except EOFError:
        fail("Не удалось прочитать путь из консоли")


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            signature = stream.read(2)
    except OSError as exc:
        fail(f"Не удалось открыть SARIF {path}: {exc}")
    if signature == b"\x1f\x8b":
        fail(
            f"{path.name} — сжатый .gz-файл. Для второго поля нужен экспорт "
            "SARIF (.sarif), а не Markup2-файл разметки (.gz)"
        )
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            value = json.load(stream)
    except UnicodeDecodeError as exc:
        fail(
            f"{path.name} не является текстовым SARIF в UTF-8. "
            f"Выберите файл, полученный через «Экспорт SARIF»: {exc}"
        )
    except (OSError, json.JSONDecodeError) as exc:
        fail(f"Не удалось прочитать SARIF {path}: {exc}")
    if not isinstance(value, dict):
        fail(f"В {path} ожидался JSON-объект")
    if value.get("version") != "2.1.0" or not isinstance(value.get("runs"), list):
        fail(
            f"{path.name} не похож на SARIF 2.1.0: отсутствуют version=2.1.0 или runs"
        )
    return value


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    decoded: str | None = None
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            decoded = path.read_text(encoding=encoding)
            break
        except (OSError, UnicodeDecodeError) as exc:
            last_error = exc
    if decoded is None:
        fail(f"Не удалось прочитать CSV {path}: {last_error}")

    try:
        dialect = csv.Sniffer().sniff(decoded[:8192], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(decoded.splitlines(), dialect=dialect)
    if reader.fieldnames is None:
        fail(f"В CSV {path} отсутствует строка заголовков")

    by_lower = {name.strip().lower(): name for name in reader.fieldnames}
    missing = [name for name in REQUIRED_CSV_COLUMNS if name.lower() not in by_lower]
    if missing:
        fail(
            f"В CSV отсутствуют столбцы: {', '.join(missing)}. "
            f"Найдены: {', '.join(reader.fieldnames)}"
        )

    rows: list[dict[str, str]] = []
    for row_number, raw in enumerate(reader, 2):
        row = {
            name: str(raw.get(by_lower[name.lower()]) or "").strip()
            for name in REQUIRED_CSV_COLUMNS
        }
        if not any(row.values()):
            continue
        if not row["Checker"] or not row["File"] or not row["Line"]:
            fail(f"CSV, строка {row_number}: Checker, File и Line обязательны")
        try:
            line = int(row["Line"])
        except ValueError:
            fail(f"CSV, строка {row_number}: Line должен быть числом, получено {row['Line']!r}")
        if line < 1:
            fail(f"CSV, строка {row_number}: Line должен быть положительным")
        row["Line"] = str(line)
        row["_csv_row"] = str(row_number)
        rows.append(row)
    if not rows:
        fail(f"CSV {path} не содержит маркеров")
    return rows


def normalize_path(value: str) -> str:
    return unquote(value).replace("\\", "/")


def repo_path(value: str | None, strip_prefix: str | None) -> str | None:
    if not value or not strip_prefix:
        return None
    normalized = normalize_path(value)
    prefix = normalize_path(strip_prefix)
    if not prefix.endswith("/"):
        prefix += "/"
    if not normalized.startswith(prefix):
        return None
    relative = normalized[len(prefix) :].lstrip("/")
    return str(PurePosixPath(relative)) if relative else None


def detect_strip_prefix(csv_rows: list[dict[str, str]]) -> str | None:
    files = [normalize_path(row["File"]) for row in csv_rows]
    return next(
        (prefix for prefix in KNOWN_SCAN_PREFIXES if all(path.startswith(prefix) for path in files)),
        None,
    )


def primary_location(result: dict[str, Any]) -> tuple[str, int]:
    try:
        physical = result["locations"][0]["physicalLocation"]
        file_name = normalize_path(str(physical["artifactLocation"]["uri"]))
        line = int(physical["region"]["startLine"])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        fail(f"В SARIF-маркере отсутствует основная file:line локация: {exc}")
    return file_name, line


def marker_key(checker: str, file_name: str, line: int | str) -> tuple[str, str, int]:
    return checker.strip(), normalize_path(file_name.strip()), int(line)


def result_key(result: dict[str, Any]) -> tuple[str, str, int]:
    file_name, line = primary_location(result)
    checker = str((result.get("properties") or {}).get("warnClass") or "").strip()
    if not checker:
        fail(f"SARIF-маркер {file_name}:{line} не содержит properties.warnClass")
    return checker, file_name, line


def effective_severity(result: dict[str, Any]) -> str:
    properties = result.get("properties") or {}
    reviewed = str(properties.get("severity") or "").strip()
    if reviewed and reviewed.lower() != "unspecified":
        return reviewed
    return str(properties.get("checker_severity") or "").strip()


def collect_results(sarif: dict[str, Any]) -> list[dict[str, Any]]:
    runs = sarif.get("runs")
    if not isinstance(runs, list) or not runs:
        fail("SARIF не содержит runs")
    collected: list[dict[str, Any]] = []
    for run_index, run in enumerate(runs):
        if not isinstance(run, dict):
            continue
        for result_index, result in enumerate(run.get("results") or []):
            if not isinstance(result, dict):
                continue
            collected.append(
                {
                    "run_index": run_index,
                    "result_index": result_index,
                    "result": result,
                    "run": run,
                }
            )
    if not collected:
        fail("SARIF не содержит results")
    return collected


def select_results(
    references: list[dict[str, Any]], csv_rows: list[dict[str, str]]
) -> list[dict[str, Any]]:
    """Match every CSV row exactly once, retaining CSV order and duplicates."""
    candidates: dict[tuple[str, str, int], list[dict[str, Any]]] = defaultdict(list)
    for reference in references:
        candidates[result_key(reference["result"])].append(reference)

    rows_by_key: dict[tuple[str, str, int], list[dict[str, str]]] = defaultdict(list)
    for row in csv_rows:
        rows_by_key[marker_key(row["Checker"], row["File"], row["Line"])].append(row)

    queues: dict[tuple[str, str, int], deque[dict[str, Any]]] = {}
    severity_queues: dict[
        tuple[str, str, int], dict[str, deque[dict[str, Any]]]
    ] = {}
    errors: list[str] = []

    for key, key_rows in rows_by_key.items():
        key_candidates = candidates.get(key, [])
        if len(key_candidates) < len(key_rows):
            errors.append(
                f"{key[0]} {key[1]}:{key[2]} — в CSV {len(key_rows)}, "
                f"в SARIF {len(key_candidates)}"
            )
            continue
        if len(key_candidates) == len(key_rows):
            queues[key] = deque(key_candidates)
            continue

        rows_by_severity = Counter(row["Severity"].casefold() for row in key_rows)
        candidates_by_severity: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate in key_candidates:
            candidates_by_severity[effective_severity(candidate["result"]).casefold()].append(
                candidate
            )
        if all(
            len(candidates_by_severity[severity]) == count
            for severity, count in rows_by_severity.items()
        ):
            severity_queues[key] = {
                severity: deque(items)
                for severity, items in candidates_by_severity.items()
                if severity in rows_by_severity
            }
        else:
            errors.append(
                f"{key[0]} {key[1]}:{key[2]} — CSV не содержит invariant, "
                f"поэтому {len(key_rows)} строк нельзя однозначно выбрать из "
                f"{len(key_candidates)} SARIF-маркеров"
            )

    if errors:
        fail("Не удалось однозначно сопоставить CSV с SARIF:\n- " + "\n- ".join(errors))

    selected: list[dict[str, Any]] = []
    for row in csv_rows:
        key = marker_key(row["Checker"], row["File"], row["Line"])
        if key in severity_queues:
            severity = row["Severity"].casefold()
            queue = severity_queues[key].get(severity)
            if not queue:
                fail(f"Внутренняя ошибка сопоставления severity для CSV-строки {row['_csv_row']}")
            selected.append(queue.popleft())
        else:
            selected.append(queues[key].popleft())
    return selected


def normalize_logical_locations(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        result.append(
            compact(
                {
                    "name": item.get("name"),
                    "fully_qualified_name": item.get("fullyQualifiedName"),
                    "decorated_name": item.get("decoratedName"),
                    "kind": item.get("kind"),
                }
            )
        )
    return result


def normalize_location(location: dict[str, Any], strip_prefix: str | None) -> dict[str, Any]:
    physical = location.get("physicalLocation") or {}
    artifact = physical.get("artifactLocation") or {}
    region = physical.get("region") or {}
    file_name = text(artifact.get("uri"))
    normalized_file = normalize_path(file_name) if file_name else None
    snippet = region.get("snippet") or {}
    return compact(
        {
            "file": normalized_file,
            "repo_path": repo_path(normalized_file, strip_prefix),
            "line": region.get("startLine"),
            "column": region.get("startColumn"),
            "end_line": region.get("endLine"),
            "end_column": region.get("endColumn"),
            "source_language": region.get("sourceLanguage"),
            "snippet": text(snippet),
            "message": text(location.get("message")),
            "logical_locations": normalize_logical_locations(location.get("logicalLocations")),
        }
    )


def normalize_trace(result: dict[str, Any], strip_prefix: str | None) -> list[dict[str, Any]]:
    flows: list[dict[str, Any]] = []
    for flow_number, code_flow in enumerate(result.get("codeFlows") or [], 1):
        threads: list[dict[str, Any]] = []
        for thread_number, thread_flow in enumerate(code_flow.get("threadFlows") or [], 1):
            steps: list[dict[str, Any]] = []
            for step_number, step in enumerate(thread_flow.get("locations") or [], 1):
                location = step.get("location") or {}
                normalized = normalize_location(location, strip_prefix)
                normalized.update(
                    compact(
                        {
                            "step": step_number,
                            "nesting_level": step.get("nestingLevel"),
                            "importance": step.get("importance"),
                            "execution_order": step.get("executionOrder"),
                            "kinds": step.get("kinds"),
                            "state": step.get("state"),
                        }
                    )
                )
                steps.append(normalized)
            threads.append(
                compact(
                    {
                        "thread": thread_number,
                        "role": text(thread_flow.get("message")),
                        "steps": steps,
                    }
                )
            )
        flows.append(
            compact(
                {
                    "flow": flow_number,
                    "role": text(code_flow.get("message")),
                    "threads": threads,
                }
            )
        )
    return flows


def rule_for(reference: dict[str, Any]) -> dict[str, Any]:
    result = reference["result"]
    driver = ((reference["run"].get("tool") or {}).get("driver") or {})
    rule: dict[str, Any] = {}
    rule_index = result.get("ruleIndex")
    rules = driver.get("rules") or []
    if isinstance(rule_index, int) and 0 <= rule_index < len(rules):
        candidate = rules[rule_index]
        if isinstance(candidate, dict):
            rule = candidate
    if not rule:
        wanted = result.get("ruleId")
        rule = next(
            (candidate for candidate in rules if isinstance(candidate, dict) and candidate.get("id") == wanted),
            {},
        )
    properties = rule.get("properties") or {}
    cwe_ids = [
        str(item.get("name"))
        for item in properties.get("cwe") or []
        if isinstance(item, dict) and item.get("name") is not None
    ]
    return compact(
        {
            "id": result.get("ruleId") or rule.get("id"),
            "name": rule.get("name"),
            "short_description": text(rule.get("shortDescription")),
            "help_uri": rule.get("helpUri"),
            "origin": properties.get("origin"),
            "cwe_ids": cwe_ids,
        }
    )


def scan_metadata(sarif: dict[str, Any]) -> dict[str, Any]:
    properties = sarif.get("properties") or {}
    return compact(
        {
            "project": properties.get("project_name"),
            "project_id": properties.get("project_id"),
            "branch": properties.get("branch_name"),
            "branch_id": properties.get("branch_id"),
            "snapshot": properties.get("snapshot_name"),
            "snapshot_id": properties.get("snapshot_id"),
            "svace_version": properties.get("checkers_config_version"),
        }
    )


def normalized_record(
    ordinal: int,
    row: dict[str, str],
    reference: dict[str, Any],
    metadata: dict[str, Any],
    strip_prefix: str | None,
) -> dict[str, Any]:
    result = reference["result"]
    properties = result.get("properties") or {}
    file_name, line = primary_location(result)
    review = {name: properties.get(name) for name in REVIEW_PROPERTY_NAMES}
    fingerprints = dict(result.get("fingerprints") or {})
    partial_fingerprints = dict(result.get("partialFingerprints") or {})
    if fingerprints.get("invariant") == properties.get("invariant"):
        fingerprints.pop("invariant", None)
    if partial_fingerprints.get("details") == properties.get("details"):
        partial_fingerprints.pop("details", None)
    locations = [
        normalize_location(location, strip_prefix)
        for location in result.get("locations") or []
        if isinstance(location, dict)
    ]
    return compact(
        {
            "schema_version": SCHEMA_VERSION,
            "triage_item_id": f"gost-{ordinal:04d}",
            "input": {
                "csv_row": int(row["_csv_row"]),
                "severity": row["Severity"],
                "checker": row["Checker"],
                "file": normalize_path(row["File"]),
                "line": int(row["Line"]),
            },
            "scan": metadata,
            "sarif_position": {
                "run": reference["run_index"],
                "result": reference["result_index"],
            },
            "identity": {
                "invariant": properties.get("invariant"),
                "details": properties.get("details"),
                "additional_fingerprints": fingerprints,
                "additional_partial_fingerprints": partial_fingerprints,
            },
            "detector": {
                "checker": properties.get("warnClass"),
                "mtid": properties.get("mtid"),
                "tool": properties.get("tool"),
                "language": properties.get("lang"),
                "checker_severity": properties.get("checker_severity"),
                "checker_reliability": properties.get("checker_reliability"),
                "tags": properties.get("tags"),
                "rule": rule_for(reference),
            },
            "finding": {
                "description": text(result.get("message")),
                "function": properties.get("origFunc"),
                "sarif_level": result.get("level"),
                "sarif_kind": result.get("kind"),
                "primary_location": locations[0]
                if locations
                else {
                    "file": file_name,
                    "repo_path": repo_path(file_name, strip_prefix),
                    "line": line,
                },
                "related_locations": locations[1:],
            },
            "trace": normalize_trace(result, strip_prefix),
            "review_before": review,
            "sarif_controls": {
                "baseline_state": result.get("baselineState"),
                "suppressions": result.get("suppressions"),
            },
        }
    )


def atomic_write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
                stream.write("\n")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def default_summary_path(output: Path) -> Path:
    return output.with_name(output.name + ".summary.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Сопоставить CSV списка ГОСТ-маркеров с полным SARIF и создать "
            "компактный JSONL с полными трассами"
        )
    )
    parser.add_argument("--csv", help="CSV, скачанный при активном фильтре ГОСТ")
    parser.add_argument("--sarif", help="Полный SARIF того же снимка")
    parser.add_argument("--out", help="Выходной JSONL; по умолчанию рядом с CSV")
    parser.add_argument(
        "--strip-prefix",
        help="Необязательный префикс пути сканера для получения repo_path, например /app/",
    )
    parser.add_argument(
        "--summary",
        help="Путь к сводке; по умолчанию <out>.summary.json",
    )
    parser.add_argument("--force", action="store_true", help="Разрешить перезапись выходных файлов")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        csv_path = path_from_paste(args.csv) if args.csv else prompt_path("Вставьте путь к CSV ГОСТ-маркеров: ")
        sarif_path = path_from_paste(args.sarif) if args.sarif else prompt_path("Вставьте путь к SARIF снимка: ")
        output_path = (
            path_from_paste(args.out)
            if args.out
            else csv_path.with_name(f"{csv_path.stem}.for-ai.jsonl")
        )
        summary_path = (
            path_from_paste(args.summary)
            if args.summary
            else default_summary_path(output_path)
        )
        for source in (csv_path, sarif_path):
            if not source.is_file():
                fail(f"Файл не найден: {source}")
        if output_path == summary_path:
            fail("--out и --summary должны указывать разные файлы")
        existing = [path for path in (output_path, summary_path) if path.exists()]
        if existing and not args.force:
            fail("Выходной файл уже существует: " + ", ".join(str(path) for path in existing))

        csv_rows = read_csv_rows(csv_path)
        strip_prefix = args.strip_prefix or detect_strip_prefix(csv_rows)
        sarif = read_json(sarif_path)
        references = collect_results(sarif)
        selected = select_results(references, csv_rows)
        metadata = scan_metadata(sarif)
        records = [
            normalized_record(index, row, reference, metadata, strip_prefix)
            for index, (row, reference) in enumerate(zip(csv_rows, selected), 1)
        ]
        duplicate_count = sum(
            count - 1
            for count in Counter(
                marker_key(row["Checker"], row["File"], row["Line"]) for row in csv_rows
            ).values()
            if count > 1
        )
        summary = {
            "schema_version": SCHEMA_VERSION,
            "inputs": {
                "csv": {"path": str(csv_path), "sha256": sha256_file(csv_path)},
                "sarif": {"path": str(sarif_path), "sha256": sha256_file(sarif_path)},
            },
            "scan": metadata,
            "counts": {
                "csv_rows": len(csv_rows),
                "sarif_results": len(references),
                "output_markers": len(records),
                "duplicate_rows_preserved": duplicate_count,
                "trace_steps": sum(
                    len(thread.get("steps") or [])
                    for record in records
                    for flow in record.get("trace") or []
                    for thread in flow.get("threads") or []
                ),
            },
            "path_mapping": {
                "strip_prefix": strip_prefix,
                "mode": "explicit" if args.strip_prefix else ("auto" if strip_prefix else "none"),
            },
            "output": {"path": str(output_path)},
        }
        atomic_write_jsonl(output_path, records)
        atomic_write_json(summary_path, summary)
    except ExtractError as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"ОШИБКА записи файла: {exc}", file=sys.stderr)
        return 2

    print(f"CSV-маркеров: {len(csv_rows)}")
    print(f"Сопоставлено: {len(records)}")
    print(f"Шагов трасс: {summary['counts']['trace_steps']}")
    if summary["path_mapping"]["strip_prefix"]:
        print(
            "Префикс путей: "
            f"{summary['path_mapping']['strip_prefix']} ({summary['path_mapping']['mode']})"
        )
    print(f"JSONL: {output_path}")
    print(f"Сводка: {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
