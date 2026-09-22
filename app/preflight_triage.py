"""Read-only Svacer preflight. Saves diagnostics, never launches a model or imports markup."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

from codex_run import (_marker_source_locations, _repository_has_source, safe_error_kind,
                       _run_checked, fetch_external_sources, now_iso)
from triage_dashboard import call_mcp_tool, read_json, atomic_json
from triage_queue import load_inventory, load_decisions, state


def preflight(job: Path, app: Path) -> dict:
    metadata = read_json(job / "job.json")
    inventory = load_inventory(job / "markers.inventory.json")
    state(inventory, load_decisions(job / "decisions.jsonl"))
    repository = job / "repository"
    revision = _run_checked(["git", "-C", str(repository), "rev-parse", "HEAD"]).stdout.strip()
    git_ref = str(metadata["git_ref"])
    refs = _run_checked(["git", "-C", str(repository), "ls-remote", "origin",
                         f"refs/tags/{git_ref}", f"refs/tags/{git_ref}^{{}}", f"refs/heads/{git_ref}"], timeout=45).stdout.splitlines()
    ref_values = {line.split()[1]: line.split()[0] for line in refs if len(line.split()) == 2}
    expected = ref_values.get(f"refs/tags/{git_ref}^{{}}") or ref_values.get(f"refs/tags/{git_ref}") or ref_values.get(f"refs/heads/{git_ref}")
    if not expected:
        raise RuntimeError("Не удалось независимо подтвердить выбранную ревизию remote")
    if revision != expected:
        raise RuntimeError("Checkout не соответствует выбранной ревизии")
    if _run_checked(["git", "-C", str(repository), "status", "--porcelain", "--untracked-files=no"]).stdout.strip():
        raise RuntimeError("Checkout содержит изменения: проверка точной ревизии невозможна")
    token = os.getenv("SVACER_LOCAL_MCP_TOKEN", "")
    if not token:
        raise RuntimeError("Выполните вход в Svacer в приложении")
    mcp_url = str(read_json(app / "svacer-settings.json").get("mcp_url") or "http://127.0.0.1:8002/mcp")
    output = job / "preflight"
    output.mkdir(exist_ok=True)
    groups = {}
    for marker in inventory:
        groups.setdefault((marker["warnClass"], marker["file"]), set()).add(str(marker["id"]))
    by_id = {}
    # Same bounded requests as the runner; large whole-snapshot trace requests
    # can fail at the Svacer server even when individual trace groups are readable.
    for index, ((detector, file_path), ids) in enumerate(groups.items(), 1):
        group = json.loads(asyncio.run(call_mcp_tool(mcp_url, token, "get_markers", {
            "project_id": metadata["project_id"], "branch_id": metadata["branch_id"],
            "snapshot_id": metadata["snapshot_id"], "advanced_filter": metadata["advanced_filter"],
            "warnClass": [detector], "file": [file_path],
            "traces": True, "checker_info": True, "fields": ["*"], "limit": 0,
        })))
        rows = group.get("markers", [])
        if group.get("truncated") is not False or not ids.issubset({str(row["id"]) for row in rows}):
            raise RuntimeError(f"Неполная трасса группы {index}: {file_path}")
        by_id.update((str(row["id"]), row) for row in rows)
        atomic_json(output / f"trace-group-{index:03d}.json", group)
        print(f"Trace groups checked: {index}/{len(groups)}", flush=True)
    payload = {"markers": list(by_id.values()), "truncated": False, "total_count": len(by_id),
               "returned_count": len(by_id), "filters_applied": {"advanced_filter": metadata["advanced_filter"]}}
    atomic_json(output / "markers-with-traces.json", payload)
    traced = load_inventory(output / "markers-with-traces.json")
    expected_ids = {str(row["id"]) for row in inventory}
    if {str(row["id"]) for row in traced} != expected_ids:
        raise RuntimeError("Набор маркеров снимка изменился относительно инвентаря")
    locations = _marker_source_locations([payload], expected_ids)
    external, errors = fetch_external_sources(job, repository, metadata["snapshot_id"],
                                               [payload], expected_ids, mcp_url, token)
    missing_traces = [str(row["id"]) for row in traced if not row.get("traces")]
    report = {
        "checked_at": now_iso(), "snapshot_id": metadata["snapshot_id"], "revision": revision,
        "marker_count": len(traced), "trace_file_count": len(locations),
        "local_file_count": sum(_repository_has_source(repository, path) for path in locations),
        "snapshot_file_count": len(external), "source_errors": errors, "missing_traces": missing_traces,
        "initial_context_available": not errors and not missing_traces,
        "classification_guaranteed": False,
        "scope": "Проверены файлы трасс. Дополнительные вызовы, provider-ы, generated data и доказательства проверяются отдельно для каждого маркера.",
    }
    atomic_json(output / "report.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("job", type=Path)
    args = parser.parse_args()
    try:
        print(json.dumps(preflight(args.job.resolve(), Path(__file__).resolve().parent), ensure_ascii=True))
    except (Exception, SystemExit) as exc:
        failure = {"preflight_failed": safe_error_kind(exc), "classification_guaranteed": False}
        output = args.job.resolve() / "preflight"
        output.mkdir(exist_ok=True)
        atomic_json(output / "report.json", failure)
        print(json.dumps(failure, ensure_ascii=True))
        raise SystemExit(1)
