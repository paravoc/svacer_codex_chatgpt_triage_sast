"""Deterministic evidence checks, not a substitute for semantic code review."""
from __future__ import annotations

import copy
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
        from dependency_sources import dependency_text
        content = dependency_text(job, context, file_path)
        if content is not None:
            return content
        raise ValueError("source is not present in the verified context")
    return path.read_text(encoding="utf-8-sig")


def normalized_lines(text: str) -> str:
    return "\n".join(line.strip() for line in text.strip().splitlines())


def repair_source_evidence_ranges(
    job: Path, context: dict[str, Any], row: dict[str, Any], *, radius: int = 2,
) -> dict[str, Any]:
    """Repair source-viewer numbering or a uniquely proven nearby range typo.

    Viewer prefixes are removed only when every line number matches the stated
    range and every remaining line matches the verified source. Code is never
    changed or invented. A range is adjusted only
    when the complete quote has one unique match within ``radius`` lines of the
    claimed start and still fits the 99-line evidence contract.
    """
    refs = row.get("source_evidence")
    if not isinstance(refs, list):
        return row
    repaired = copy.deepcopy(row)
    changed = False
    for ref in repaired["source_evidence"]:
        if not isinstance(ref, dict):
            continue
        path, start, end, quote = (ref.get(key) for key in (
            "file_path", "line_start", "line_end", "excerpt",
        ))
        if (not isinstance(path, str) or type(start) is not int or type(end) is not int
                or start < 1 or not isinstance(quote, str) or not quote.strip()):
            continue
        quote_lines = quote.strip().splitlines()
        if not 1 <= len(quote_lines) <= 99:
            continue
        try:
            source_lines = source_text(job, context, path).splitlines()
        except (OSError, ValueError, KeyError):
            continue
        target = normalized_lines(quote)
        claimed = "\n".join(source_lines[start - 1:end]) if end <= len(source_lines) else ""
        if normalized_lines(claimed) == target:
            continue
        numbered = [re.fullmatch(r"([1-9][0-9]*): ?(.*)", line) for line in quote_lines]
        if (end - start + 1 == len(quote_lines) and all(numbered)
                and [int(match[1]) for match in numbered] == list(range(start, end + 1))
                and normalized_lines("\n".join(match[2] for match in numbered)) == normalized_lines(claimed)):
            ref["excerpt"] = claimed
            changed = True
            continue
        first = max(0, start - 1 - radius)
        last = min(len(source_lines) - len(quote_lines), start - 1 + radius)
        matches = [
            index for index in range(first, last + 1)
            if normalized_lines("\n".join(source_lines[index:index + len(quote_lines)])) == target
        ]
        if len(matches) != 1:
            continue
        actual_start = matches[0] + 1
        actual_end = actual_start + len(quote_lines) - 1
        if abs(actual_end - end) > radius:
            continue
        ref["line_start"] = actual_start
        ref["line_end"] = actual_end
        changed = True
    return repaired if changed else row


def review_result(job: Path, context: dict[str, Any], row: dict[str, Any]) -> list[str]:
    errors = []
    if row.get("decision_policy_version") == 3 or row.get("disposition_kind") == "scope_exclusion":
        from analysis_scope import validate_scope_result
        errors.extend(validate_scope_result(job, context, row))
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
            # ValueError messages here explain source identity/range failures;
            # never include source contents or arbitrary OS error details.
            detail = f" ({exc})" if isinstance(exc, ValueError) else ""
            errors.append(f"source_evidence[{index}]: {type(exc).__name__}: recheck {path}:{start}-{end}{detail}")
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
