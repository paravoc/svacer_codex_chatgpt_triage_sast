"""Deterministic evidence checks, not a substitute for semantic code review."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


UNRESOLVED = re.compile(
    r"не (?:удалось|смог(?:ла|ли)?)|нужна проверка|требуется (?:проверить|уточнить|найти)|"
    r"(?:недостаточно|не хватает) (?:данных|доказательств)|данные не доказывают|"
    r"(?:продуктов[а-я]*|прям[а-я]*)\s+вызов[а-я]*\s+не\s+найден[а-я]*|"
    r"исходник[а-я]*\s+(?:не найден[а-я]*|отсутству[а-я]*)|"
    r"скорее всего|вероятно|(?:could not|couldn't|unable to) (?:find|verify|locate)|"
    r"(?:need|requires?) (?:further )?(?:verification|investigation)|insufficient evidence",
    re.IGNORECASE,
)
# These four roles are the proof contract. ``entrypoint`` is useful additional
# metadata and is accepted when an analyst labels a product entry point
# explicitly, but it never replaces any required role below.
ROLES = {"source", "sink", "control", "product_reachability"}
OPTIONAL_ROLES = {"entrypoint"}
ALLOWED_ROLES = ROLES | OPTIONAL_ROLES


def safe_source_path(path: str) -> bool:
    parts = path.replace("\\", "/").casefold().split("/")
    return bool(path) and not any(
        part in {"..", ".env", ".netrc", ".ssh", ".aws", ".git", "credentials.json", "auth.json",
                 "id_rsa", "id_ed25519"} or part.startswith(".env.")
        or part.endswith((".pem", ".key", ".p12", ".pfx")) for part in parts
    )


def repository_source(repository: Path, file_path: str) -> Path | None:
    if not safe_source_path(file_path):
        return None
    normalized = file_path.replace("\\", "/")
    candidates = [Path(file_path), repository / normalized.lstrip("/")]
    for prefix in ("/src/src/", "/execroot/envoy/", "/envoy/"):
        if prefix in normalized:
            candidates.append(repository / normalized.split(prefix, 1)[1])
    root = repository.resolve()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_relative_to(root) and resolved.is_file():
            return resolved
    return None


def source_text(job: Path, context: dict[str, Any], file_path: str) -> str:
    if not safe_source_path(file_path):
        raise ValueError("unsafe source path")
    # Snapshot text is preferred: its line numbers belong to this exact finding.
    for source in [*context.get("external_sources", []), *context.get("source_catalog", [])]:
        if source.get("file_path") != file_path:
            continue
        saved_path = (job / str(source["local_path"])).resolve()
        if not saved_path.is_relative_to((job / "external-sources").resolve()):
            raise ValueError("preview is outside the external-source directory")
        saved = json.loads(saved_path.read_text(encoding="utf-8-sig"))
        if saved.get("snapshot_id") != context.get("snapshot_id") or saved.get("file_path") != file_path:
            raise ValueError("preview belongs to another snapshot or file")
        preview = saved.get("preview", {})
        content = preview.get("content")
        total = preview.get("total_lines")
        if (not isinstance(content, str) or not content.strip() or type(total) is not int
                or total < 1 or preview.get("line") not in (0, 1) or len(content.split("\n")) < total):
            raise ValueError("source preview is incomplete")
        return content
    path = repository_source(Path(context["repository"]), file_path)
    if path is None:
        raise ValueError("source is not present in the verified context")
    return path.read_text(encoding="utf-8-sig")


def normalized_lines(text: str) -> str:
    return "\n".join(line.strip() for line in text.strip().splitlines())


def review_result(job: Path, context: dict[str, Any], row: dict[str, Any]) -> list[str]:
    errors = []
    if row.get("review_contract_version") != 1:
        errors.append("review_contract_version=1 is required")
    if row.get("source_revision") != context.get("revision"):
        errors.append("source_revision does not match the analyzed revision")
    comment = row.get("comment", "")
    if not isinstance(comment, str) or not 30 <= len(comment.strip()) <= 1800:
        errors.append("comment must be a concrete 30-1800 character explanation in Russian")
    elif UNRESOLVED.search(comment):
        errors.append("comment describes unfinished research; gather evidence or save needs_context")
    refs = row.get("source_evidence")
    if not isinstance(refs, list) or not 1 <= len(refs) <= 40:
        return errors + ["source_evidence requires 1-40 exact line ranges with verbatim excerpts"]
    roles = set()
    linked = False
    for index, ref in enumerate(refs, 1):
        if not isinstance(ref, dict):
            errors.append(f"source_evidence[{index}]: expected an object")
            continue
        path, start, end, quote = (ref.get(key) for key in ("file_path", "line_start", "line_end", "excerpt"))
        ref_roles = ref.get("roles")
        if not isinstance(path, str) or not path.strip():
            errors.append(f"source_evidence[{index}]: file_path must be a non-empty string")
            continue
        if (type(start) is not int or type(end) is not int
                or start < 1 or not start <= end < start + 100):
            errors.append(f"source_evidence[{index}]: expected a 1-99 line range with line_start <= line_end")
            continue
        if not isinstance(quote, str) or not quote.strip():
            errors.append(f"source_evidence[{index}]: excerpt must contain a verbatim quote")
            continue
        if not isinstance(ref_roles, list) or not ref_roles or any(not isinstance(role, str) for role in ref_roles):
            errors.append(f"source_evidence[{index}]: roles must be a non-empty string array")
            continue
        unknown_roles = sorted(set(ref_roles) - ALLOWED_ROLES)
        if unknown_roles:
            errors.append(
                f"source_evidence[{index}]: unknown roles: {', '.join(unknown_roles)}; "
                f"allowed: {', '.join(sorted(ALLOWED_ROLES))}"
            )
            continue
        if not isinstance(ref.get("supports"), str) or not ref["supports"].strip():
            errors.append(f"source_evidence[{index}]: supports must explain the proven fact")
            continue
        try:
            lines = source_text(job, context, path).splitlines()
            if end > len(lines) or normalized_lines("\n".join(lines[start-1:end])) != normalized_lines(quote):
                raise ValueError("excerpt does not match the stated source lines")
        except (OSError, ValueError, KeyError) as exc:
            errors.append(f"source_evidence[{index}]: {type(exc).__name__}: recheck {path}:{start}-{end}")
            continue
        roles.update(set(ref_roles) & ROLES)
        # A comment must point to at least one checked source location.
        filename = Path(path.replace("\\", "/")).name
        if isinstance(comment, str):
            citations = re.finditer(re.escape(filename) + r":([1-9]\d*)(?!\d)", comment)
            if any(start <= int(match.group(1)) <= end for match in citations):
                linked = True
    if ROLES - roles:
        errors.append("evidence does not cover: " + ", ".join(sorted(ROLES - roles)))
    if not linked:
        errors.append("comment must cite at least one verified file:line")
    return errors
