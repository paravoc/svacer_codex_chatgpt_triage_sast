"""Explicit product-scope dispositions, separate from defect/safety verdicts.

No network or model calls. Only an opt-in job and a positively classified,
checksum-pinned build-only dependency can use the deterministic exclusion.
Unknown/mixed-use dependencies continue through normal source analysis.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

FULL_SCOPE = "product_and_tooling"
SHIPPED_SCOPE = "shipped_product"
SCOPE_LABELS = {
    FULL_SCOPE: "Продукт, сборка и инструменты",
    SHIPPED_SCOPE: "Только поставляемый продукт",
}


def build_only_result(job: Path, context: dict, marker: dict) -> dict | None:
    """Create a disposition, NEVER a claim that an uninvestigated defect exists."""
    from decision_quality import source_text
    from dependency_sources import dependency_name, dependency_text

    metadata = json.loads((job / "job.json").read_text(encoding="utf-8-sig"))
    if (metadata.get("analysis_scope", FULL_SCOPE) != SHIPPED_SCOPE
            or not context.get("revision") or not context.get("snapshot_id")
            or metadata.get("git_commit") != context["revision"]
            or metadata.get("snapshot_id") != context["snapshot_id"]):
        return None
    path, line = marker.get("file"), marker.get("line")
    if not isinstance(path, str) or type(line) is not int or line < 1:
        return None
    name = dependency_name(path)
    if not name:
        return None
    recipe_path = "bazel/repository_locations.bzl"
    try:
        recipe = source_text(job, context, recipe_path)
        tree = ast.parse(recipe)
        definitions = [n.value for n in tree.body if isinstance(n, ast.Assign)
                       and any(isinstance(t, ast.Name) and t.id == "REPOSITORY_LOCATIONS_SPEC"
                               for t in n.targets)]
        if len(definitions) != 1 or not isinstance(definitions[0], ast.Call):
            return None
        entries = [k.value for k in definitions[0].keywords if k.arg == name]
        if len(entries) != 1 or not isinstance(entries[0], ast.Call):
            return None
        node = entries[0]
        fields = {k.arg: ast.literal_eval(k.value) for k in node.keywords
                  if k.arg in {"use_category", "version", "sha256"}}
        # Never exclude build + runtime/API/test categories by substring matching.
        if fields.get("use_category") != ["build"]:
            return None
        records = [r for r in context.get("dependency_sources", []) if r.get("name") == name
                   and r.get("version") == fields.get("version")
                   and r.get("archive_sha256") == fields.get("sha256")
                   and r.get("product_revision") == context["revision"]
                   and r.get("snapshot_id") == context["snapshot_id"]
                   and r.get("snapshot_matches") and not r.get("snapshot_conflicts")]
        if not records:
            return None
        verified = dependency_text(job, {**context, "dependency_sources": records}, path)
        text = source_text(job, context, path)
        if verified is None or verified.splitlines() != text.splitlines():
            return None
        lines = text.splitlines()
        if line > len(lines) or node.end_lineno - node.lineno >= 99:
            return None
    except (OSError, ValueError, SyntaxError, KeyError, TypeError):
        return None
    start, end = max(1, line - 4), min(len(lines), line + 3)
    reason = (f"Область разметки — поставляемый продукт. {name} {fields['version']} "
              f"явно отнесён к отдельным зависимостям сборки (use_category=[\"build\"]) "
              f"в {recipe_path}:{node.lineno}. Такой инструмент исключён выбранной пользователем "
              "политикой; безопасность самого инструмента этим решением не утверждается.")
    # Publication text is separate from the internal explanation of the policy.
    display_name = next((k.value.value.strip() for k in node.keywords
                         if k.arg == "project_name" and isinstance(k.value, ast.Constant)
                         and isinstance(k.value.value, str) and k.value.value.strip()), name)
    comment = (f"{display_name} {fields['version']} отнесён к зависимостям только для сборки "
               f"(use_category=[\"build\"], {recipe_path}:{node.lineno}). "
               "Срабатывание относится к инструменту сборки вне области проверки поставляемого продукта.")
    return {
        "schema_version": 2, "decision_policy_version": 3, "review_contract_version": 1,
        "marker_id": str(marker.get("id") or marker.get("marker_id") or ""),
        "warnClass": marker.get("warnClass"), "file": path, "line": line,
        "source_revision": context["revision"], "analysis_status": "complete",
        "verdict": "Won't fix", "confidence": "high", "defect_scope": "out_of_scope",
        "disposition_kind": "scope_exclusion", "disposition_reason": reason,
        "component_defect_proven": None, "product_defect_reachable": None,
        "scope_exclusion": {"policy": SHIPPED_SCOPE, "dependency": name,
                            "version": fields["version"], "archive_sha256": fields["sha256"],
                            "use_category": ["build"], "snapshot_id": context["snapshot_id"]},
        "source": "Исходник отдельного инструмента сборки из точной pinned-зависимости.",
        "sink": f"Указанная анализатором операция: {Path(path).name}:{line}; дефект не подтверждался.",
        "control": "Применена явная политика области разметки, не предположение о достижимости.",
        "entrypoint": "Инструмент генерации/обслуживания сборки, исключённый из области этой задачи.",
        "build_reachability": f"Положительная классификация build-only: {recipe_path}:{node.lineno}.",
        "product_reachability": "Не оценивалась для сборки/CI: эта поверхность вне выбранной области.",
        "impact": "Не оценивался; Won't fix обозначает исключение из области, а не отсутствие риска.",
        "comment": comment, "reachable_path": [], "counterevidence": [], "proof_gaps": [],
        "evidence": [f"{recipe_path}:{node.lineno}: use_category=[\"build\"], точная версия и hash.",
                     f"{Path(path).name}:{line}: исходник маркера совпадает с проверенной зависимостью."],
        "boundary": {"product_surface": "Поставляемый продукт, без отдельных build-only инструментов",
                     "source_trust": "Точная ревизия и источник снимка; дефект компонента не оценивался",
                     "policy_basis": "Явно выбранная пользователем область shipped_product",
                     "boundary_crossed": False},
        "source_evidence": [
            {"file_path": recipe_path, "line_start": node.lineno, "line_end": node.end_lineno,
             "excerpt": "\n".join(recipe.splitlines()[node.lineno - 1:node.end_lineno]),
             "supports": "Классификация зависимости как только build; основание исключения из области, не доказательство безопасности.",
             "roles": ["control", "product_reachability"]},
            {"file_path": path, "line_start": start, "line_end": end,
             "excerpt": "\n".join(lines[start - 1:end]),
             "supports": "Место срабатывания принадлежит точной исключённой зависимости; наличие дефекта не утверждается.",
             "roles": ["source", "sink"]},
        ],
    }


def validate_scope_result(job: Path, context: dict, row: dict) -> list[str]:
    """Recompute the disposition from user policy and current verified sources."""
    try:
        expected = build_only_result(job, context, row)
    except (OSError, ValueError, TypeError):
        expected = None
    if expected is None:
        return ["scope exclusion is not authorized or lacks exact build-only source evidence"]
    # Preserve already saved results and in-flight workers using the original
    # wording. Only this exact legacy template is accepted; every proof field
    # still has to match and review_result also checks the comment's citation.
    changed = [key for key, value in expected.items() if row.get(key) != value
               and not (key == "comment" and row.get(key) == expected["disposition_reason"])]
    return ["scope exclusion differs from verified policy: " + ", ".join(changed)] if changed else []


def apply_scope_review(job: Path, context: dict, marker_id: str) -> dict:
    """Close ONE stopped, unreserved marker locally with an auditable policy review.

    Does not launch analysis, alter other reservations, or send anything to Svacer.
    The normal worker path uses the same generator/validator during a future run.
    """
    import time
    import uuid
    import codex_run as r
    import triage_queue as q
    from decision_quality import repository_source
    from local_jobs import job_operation_lock, require_idle
    from marker_history import append_batch_history

    started, clock_start = r.now_iso(), time.monotonic()
    with job_operation_lock(job), q.decision_lock(job / "decisions.jsonl"):
        require_idle(job)
        if (job / "svacer-import-attempt.json").exists():
            raise ValueError("Изменение заблокировано после попытки отправки в Svacer.")
        status = r.read_json(job / "workers.status.json") if (job / "workers.status.json").exists() else {}
        if status.get("state") == "assigned" and any(marker_id in a.get("marker_ids", [])
                                                    for a in status.get("workers", [])):
            raise ValueError("Маркер зарезервирован: продолжите его обычный анализ с выбранной областью.")
        inventory = q.load_inventory(job / "markers.inventory.json")
        marker = next((m for m in inventory if str(m.get("id")) == marker_id), None)
        decisions = q.load_decisions(job / "decisions.jsonl")
        current = next((m for m in decisions if str(m.get("marker_id")) == marker_id), None)
        if marker is None or current is None or current.get("verdict"):
            raise ValueError("Нужен известный маркер без сохранённого вердикта.")
        row = build_only_result(job, context, marker)
        if row is None:
            raise ValueError("Маркер не соответствует подтверждённому исключению из области.")
        repo = Path(context["repository"])
        revision = r._run_checked(["git", "-C", str(repo), "rev-parse", "HEAD"]).stdout.strip()
        dirty = r._run_checked(["git", "-C", str(repo), "diff", "--name-only", "HEAD", "--"]).stdout.strip()
        tracked = set(r._run_checked(["git", "-C", str(repo), "ls-files", "-z"]).stdout.split("\0"))
        if revision != context["revision"] or dirty:
            raise ValueError("Точная ревизия исходников изменилась; решение не применено.")
        for ref in row["source_evidence"]:
            path = repository_source(repo, ref["file_path"])
            if path and path.relative_to(repo.resolve()).as_posix() not in tracked:
                raise ValueError("Доказательство ссылается на файл вне зафиксированной ревизии.")
        errors = r.review_result(job, context, row) + q.validate_worker_result(row, current)
        if errors:
            raise ValueError("; ".join(errors))
        control_path, incomplete_path = job / "control.json", job / "incomplete-analysis.json"
        control = r.read_json(control_path) if control_path.exists() else {}
        incomplete = r.read_json(incomplete_path) if incomplete_path.exists() else {}
        audit_id = "scope-" + uuid.uuid4().hex
        audit = job / "scope-reviews" / audit_id
        audit.mkdir(parents=True)
        r.atomic_json(audit / "before.json", {"decision": current, "control": control,
                                             "incomplete": incomplete.get(marker_id)})
        r.atomic_json(audit / "context.json", context)
        r.atomic_json(audit / "result.json", [row])
        q.apply_worker_result_rows(decisions, [row], [marker_id], job / "decisions.jsonl",
                                   {str(m["id"]) for m in q.markers_for_triage(inventory)})
        elapsed, finished = time.monotonic() - clock_start, r.now_iso()
        review_context = {**context, "completed_marker_ids": [marker_id],
                          "batch": {"marker_ids": [marker_id], "assignments": [
                              {"worker": 0, "marker_ids": [marker_id], "markers": [marker]}]},
                          "worker_measurements": {marker_id: {"execution_kind": "scope_policy",
                              "started_at": started, "finished_at": finished, "duration_seconds": elapsed}}}
        append_batch_history(job, review_context, launch_id=audit_id, runner_batch=1,
                             started_at=started, finished_at=finished, elapsed_seconds=elapsed,
                             exit_code=0, usage={}, agent_messages=[])
        for key in ("priority_marker_ids", "recheck_marker_ids", "deferred_marker_ids"):
            if key in control:
                control[key] = [mid for mid in control[key] if mid != marker_id]
        control.update(updated_at=q.utc_now(), source="explicit scope review")
        r.atomic_json(control_path, control)  # Preserve pause_requested and all other selections.
        incomplete.pop(marker_id, None)
        if incomplete_path.exists():
            r.atomic_json(incomplete_path, incomplete)
        r.atomic_json(audit / "completed.json", {"marker_id": marker_id, "finished_at": finished})
        return {"marker_id": marker_id, "verdict": row["verdict"], "comment": row["comment"],
                "audit": str(audit), "model_calls": 0}
