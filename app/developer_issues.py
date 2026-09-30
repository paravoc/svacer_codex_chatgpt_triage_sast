"""Local developer-issue drafts and explicitly approved GitHub delivery.

This module never turns an old Confirmed/verified decision into a publishable
report on its own. Draft generation is offline; publication is a separate,
fail-closed operation with a human-reviewed evidence checklist.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
PUBLIC_LEAK = re.compile(
    r"svacer|\.internal\b|[A-Za-z]:\\Users\\|\.local\b|"
    r"(?:password|private[-_ ]?key|bearer)\s*[:=]",
    re.IGNORECASE,
)
REQUIRED_CHECKS = (
    "exact_source_checked", "build_checked", "product_path_checked",
    "counterevidence_checked", "reproduction_checked", "duplicate_checked",
    "redaction_checked", "disclosure_approved",
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return value


def decisions_by_id(path: Path) -> dict[str, dict]:
    rows: dict[str, dict] = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        mid = row.get("marker_id") if isinstance(row, dict) else None
        if not isinstance(mid, str) or not mid or mid in rows:
            raise ValueError(f"Missing or duplicate marker_id in {path}")
        rows[mid] = row
    return rows


def draft_body(job: dict, row: dict) -> str:
    component = Path(str(job.get("repository_url") or "component").rstrip("/")).stem
    version = str(job.get("git_ref") or "version not recorded")
    revision = str(job.get("git_commit") or "commit not recorded")
    location = f"{row.get('file', 'file not recorded')}:{row.get('line', '?')}"
    def field(name: str) -> str:
        value = row.get(name)
        return value.strip() if isinstance(value, str) and value.strip() else "Not documented."
    return (
        "# LOCAL DRAFT — NOT APPROVED FOR PUBLICATION\n\n"
        "The text below is an analyst hypothesis from a local SAST decision. "
        "It is not a validated developer report until source, product path, "
        "reproduction, duplicates and disclosure have been checked.\n\n"
        f"## Component and exact version\n\n{component} {version}; commit `{revision}`.\n\n"
        f"## Location\n\n`{location}`; detector `{row.get('warnClass', '?')}`.\n\n"
        f"## Suspected root cause\n\n{field('control')}\n\n"
        f"## Input and sink\n\nInput: {field('source')}\n\nSink: {field('sink')}\n\n"
        f"## Preconditions and product reachability\n\n{field('product_reachability')}\n\n"
        f"## Observed effect versus inferred impact\n\nAnalyst claim: {field('impact')}\n\n"
        "## Reproduction\n\nTODO: attach a minimal PoC, exact run command and environment, "
        "baseline observed output, negative or patched control, and repeat count. "
        "Do not call a hypothetical path a reproduced crash.\n\n"
        "## Proposed fix and regression test\n\nTODO: derive a minimal patch and test "
        "from the exact source; verify the patch changes the observed effect.\n\n"
        "## Disclosure review\n\nTODO: determine whether this is an ordinary bug "
        "or a security vulnerability. A security PoC must go through a private channel.\n"
    )


def draft_title(job: dict, row: dict) -> str:
    component = Path(str(job.get("repository_url") or "component").rstrip("/")).stem
    return f"[{component} {job.get('git_ref') or '?'}] Investigate {row.get('warnClass') or 'SAST finding'} at {Path(str(row.get('file') or '?')).name}:{row.get('line') or '?'}"


def poc_task(job: dict, row: dict) -> str:
    location = f"{row.get('file', '?')}:{row.get('line', '?')}"
    revision = job.get("git_commit") or "unknown"
    sanitizer = ("For C/C++, use ASan/UBSan where compatible."
                 if Path(str(row.get("file") or "")).suffix.lower() in {".c", ".cc", ".cpp", ".cxx"}
                 else "For Go, use go test; use -race only when the alleged defect is concurrent.")
    return (
        "# Offline PoC investigation task — not a PoC\n\n"
        f"Candidate: `{location}`; exact source revision `{revision}`.\n\n"
        "1. Verify the checkout, dependencies, build target, platform and feature flags. "
        "Do not use a different version silently.\n"
        "2. Trace source, sink, all relevant callers, guards and counterexamples. "
        "Prove a normal product entrypoint and the permissions required.\n"
        "3. Construct the smallest non-destructive input in an isolated test. "
        "Do not send it to production or an upstream repository.\n"
        f"4. Run the exact baseline binary. {sanitizer} Capture the command, "
        "environment, binary/source identity, repeat count, stdout/stderr and actual effect.\n"
        "5. Run a negative or patched control. A timeout, failed build or missing "
        "crash is not proof of False Positive; record the concrete gap.\n"
        "6. Minimize the input and link the observed effect to this exact sink. "
        "Save a textual PoC under case/poc and its SHA-256. Distinguish an "
        "internal-function-only PoC from product-level reachability.\n"
        "7. Propose a minimal patch and regression test, but do not claim the "
        "patch works until the control run confirms it.\n\n"
        "Stop with `needs-evidence` if any of steps 1–6 cannot be established. "
        "Do not change the existing SAST verdict automatically.\n"
    )


def create_drafts(campaign: Path, output: Path) -> dict[str, int]:
    plan = read_json(campaign)
    root = Path(str(plan.get("root") or campaign.parent)).resolve()
    jobs = plan.get("jobs")
    if not isinstance(jobs, list):
        raise ValueError("Campaign has no jobs")
    if output.is_symlink():
        raise ValueError("Draft output cannot be a symlink")
    selected = confirmed = created = 0
    seen: set[tuple[str, str]] = set()
    for entry in jobs:
        job_id = entry.get("job") if isinstance(entry, dict) else None
        ids = entry.get("marker_ids") if isinstance(entry, dict) else None
        if not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", job_id):
            raise ValueError("Invalid campaign job")
        if not isinstance(ids, list) or any(not isinstance(mid, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", mid) for mid in ids):
            raise ValueError(f"Invalid marker IDs in {job_id}")
        job_dir = (root / "RESULTS" / job_id).resolve()
        if not job_dir.is_relative_to(root / "RESULTS"):
            raise ValueError("Job escapes RESULTS")
        job = read_json(job_dir / "job.json")
        rows = decisions_by_id(job_dir / "decisions.jsonl")
        for mid in ids:
            if (job_id, mid) in seen or mid not in rows:
                raise ValueError(f"Duplicate or unsaved selected marker: {job_id}/{mid}")
            seen.add((job_id, mid))
            selected += 1
            row = rows[mid]
            if row.get("verdict") != "Confirmed":
                continue
            confirmed += 1
            case_dir = output / f"{job_id}--{mid}"
            if case_dir.is_symlink():
                raise ValueError("Draft case cannot be a symlink")
            case_dir.mkdir(parents=True, exist_ok=True)
            body = draft_body(job, row)
            metadata = {
                "schema_version": 1, "job_id": job_id, "marker_id": mid,
                "source_revision": job.get("git_commit"),
                "decision_sha256": digest(canonical(row)),
                "generated_body_sha256": digest(body.encode("utf-8")),
                "title": draft_title(job, row),
                "verification_status": (row.get("verification") or {}).get("status"),
                "publication_state": "blocked_pending_human_evidence_review",
            }
            review_template = {
                "target_repo": "", "channel": "", "finding_kind": "",
                "reviewer": "", "approved_title": "",
                "body_sha256": "", "decision_sha256": metadata["decision_sha256"],
                **{key: False for key in REQUIRED_CHECKS},
                "reproduction": {
                    "source_revision": "", "harness_revision": "", "environment": "",
                    "baseline_artifact": "", "control_artifact": "", "command": "",
                    "baseline_observed": "", "control_observed": "",
                    "poc_file": "", "poc_sha256": "",
                },
                "fix": {"patch_file": "", "patch_sha256": "",
                        "no_patch_reason": "", "regression_test": ""},
            }
            files = {"body.md": body,
                     "poc-task.md": poc_task(job, row),
                     "case.json": json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                     "review.template.v3.json": json.dumps(review_template, ensure_ascii=False, indent=2) + "\n"}
            for name, content in files.items():
                path = case_dir / name
                if path.exists():
                    # A template is never an approval. Keep a previously
                    # generated template if the schema evolves, and never
                    # overwrite any user-edited draft or case metadata.
                    if path.read_text(encoding="utf-8") != content:
                        raise ValueError(f"Existing draft differs; not overwriting {path}")
                else:
                    path.write_bytes(content.encode("utf-8"))
            created += 1
    return {"selected": selected, "confirmed_drafts": confirmed, "created_or_unchanged": created}


def validate_review(case_dir: Path, review: dict, metadata: dict, body: bytes,
                    row: dict, job: dict, *, repo: str, channel: str) -> list[str]:
    errors: list[str] = []
    if (not REPO.fullmatch(repo) or repo.endswith(".git")
            or any(part in {".", ".."} for part in repo.split("/"))):
        errors.append("target repo must be owner/name")
    if channel not in {"public_issue", "private_security_report"}:
        errors.append("unknown disclosure channel")
    if review.get("target_repo") != repo or review.get("channel") != channel:
        errors.append("reviewed target/channel does not match command")
    if review.get("body_sha256") != digest(body):
        errors.append("body changed after approval")
    if review.get("decision_sha256") != metadata.get("decision_sha256"):
        errors.append("decision changed after approval")
    if metadata.get("decision_sha256") != digest(canonical(row)):
        errors.append("current decision differs from draft")
    if job.get("git_commit") != metadata.get("source_revision") or row.get("source_revision") != job.get("git_commit"):
        errors.append("source revision differs")
    if row.get("verdict") != "Confirmed" or (row.get("verification") or {}).get("status") != "verified":
        errors.append("decision is not an independently verified Confirmed")
    if not isinstance(review.get("reviewer"), str) or not review["reviewer"].strip():
        errors.append("human reviewer is missing")
    errors.extend(f"{key} is not approved" for key in REQUIRED_CHECKS if review.get(key) is not True)
    kind = review.get("finding_kind")
    if kind not in {"ordinary_bug", "security_vulnerability"}:
        errors.append("finding_kind must be explicit")
    elif kind == "security_vulnerability" and channel != "private_security_report":
        errors.append("security vulnerability cannot be published as a public issue")
    elif kind == "ordinary_bug" and channel != "public_issue":
        errors.append("ordinary bug uses a public issue; choose security_vulnerability for private reporting")
    if b"TODO" in body or b"LOCAL DRAFT" in body:
        errors.append("body still contains draft placeholders")
    if PUBLIC_LEAK.search(body.decode("utf-8", errors="replace")):
        errors.append("body may contain internal hostnames, paths or credentials")
    repro = review.get("reproduction")
    if not isinstance(repro, dict):
        errors.append("reproduction record is missing")
    else:
        if repro.get("source_revision") != job.get("git_commit"):
            errors.append("PoC source revision differs from analyzed commit")
        for key in ("harness_revision", "environment", "baseline_artifact",
                    "control_artifact", "command", "baseline_observed", "control_observed"):
            if not isinstance(repro.get(key), str) or not repro[key].strip():
                errors.append(f"reproduction.{key} is missing")
        rel = repro.get("poc_file")
        checksum = repro.get("poc_sha256")
        if not isinstance(rel, str) or not rel or not isinstance(checksum, str) or not SHA256.fullmatch(checksum):
            errors.append("PoC path and SHA-256 are required")
        else:
            candidate = case_dir / rel
            poc = candidate.resolve()
            if (not poc.is_relative_to((case_dir / "poc").resolve())
                    or candidate.is_symlink() or not poc.is_file()):
                errors.append("PoC must be a regular file under case/poc")
            else:
                poc_bytes = poc.read_bytes()
                if digest(poc_bytes) != checksum:
                    errors.append("PoC changed after approval")
                elif len(poc_bytes) > 32768:
                    errors.append("PoC is too large to include in the report")
                else:
                    try:
                        poc_text = poc_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        errors.append("PoC must be UTF-8 text for GitHub report delivery")
                    else:
                        if poc_text.strip() not in body.decode("utf-8", errors="replace"):
                            errors.append("approved report body does not include the PoC")
    fix = review.get("fix")
    if not isinstance(fix, dict):
        errors.append("fix record is missing")
    else:
        if not isinstance(fix.get("regression_test"), str) or not fix["regression_test"].strip():
            errors.append("fix.regression_test is missing")
        patch_file = fix.get("patch_file")
        if patch_file:
            patch_hash = fix.get("patch_sha256")
            if not isinstance(patch_file, str) or not isinstance(patch_hash, str) or not SHA256.fullmatch(patch_hash):
                errors.append("patch path and SHA-256 are required")
            else:
                candidate = case_dir / patch_file
                patch_path = candidate.resolve()
                if (not patch_path.is_relative_to((case_dir / "patches").resolve())
                        or candidate.is_symlink() or not patch_path.is_file()):
                    errors.append("patch must be a regular file under case/patches")
                elif digest(patch_path.read_bytes()) != patch_hash:
                    errors.append("patch changed after approval")
        elif not isinstance(fix.get("no_patch_reason"), str) or not fix["no_patch_reason"].strip():
            errors.append("patch or explicit no_patch_reason is required")
    return errors


def source_contract_errors(job_dir: Path, job: dict, row: dict) -> list[str]:
    """Check exact citations using a context assigned to this marker, not the last batch."""
    from decision_quality import review_result

    mid = row["marker_id"]
    root = job_dir / "worker-runs"
    candidates: list[tuple[int, list[str]]] = []
    for path in root.rglob("context.json") if root.is_dir() else ():
        if path.is_symlink() or not path.is_file():
            continue
        try:
            context = read_json(path)
        except (OSError, ValueError):
            continue
        batch = context.get("batch")
        if (not isinstance(batch, dict) or mid not in batch.get("marker_ids", [])
                or context.get("revision") != job.get("git_commit")
                or context.get("snapshot_id") != job.get("snapshot_id")):
            continue
        errors = review_result(job_dir, context, row)
        if not errors:
            return []
        candidates.append((len(errors), errors))
    if not candidates:
        return ["no exact per-marker source context was found"]
    best = min(candidates, key=lambda item: item[0])[1]
    return ["exact source contract failed: " + "; ".join(best[:6])]


def check_case(case_dir: Path, campaign: Path, *, repo: str,
               channel: str) -> tuple[dict, bytes, dict, list[str]]:
    if case_dir.is_symlink():
        raise ValueError("Case cannot be a symlink")
    metadata = read_json(case_dir / "case.json")
    plan = read_json(campaign)
    root = Path(str(plan.get("root") or campaign.parent)).resolve()
    selected = {entry.get("job"): entry.get("marker_ids", []) for entry in plan["jobs"]}
    job_id, mid = metadata.get("job_id"), metadata.get("marker_id")
    if mid not in selected.get(job_id, []):
        raise ValueError("Case marker is outside the selected campaign")
    job_dir = (root / "RESULTS" / job_id).resolve()
    if not job_dir.is_relative_to(root / "RESULTS"):
        raise ValueError("Job escapes RESULTS")
    row = decisions_by_id(job_dir / "decisions.jsonl")[mid]
    job = read_json(job_dir / "job.json")
    body = (case_dir / "body.md").read_bytes()
    source_errors = source_contract_errors(job_dir, job, row)
    if not (case_dir / "review.json").exists():
        return metadata, body, {}, ["human review.json is missing", *source_errors]
    review = read_json(case_dir / "review.json")
    errors = validate_review(case_dir, review, metadata, body, row, job,
                             repo=repo, channel=channel)
    errors.extend(source_errors)
    if not isinstance(review.get("approved_title"), str) or not review["approved_title"].strip():
        errors.append("approved_title is missing")
    return metadata, body, review, errors


def publish(case_dir: Path, campaign: Path, *, repo: str, channel: str,
            confirmation: str) -> str:
    metadata = read_json(case_dir / "case.json")
    expected = f"PUBLISH {metadata['marker_id']} TO {repo} AS {channel}"
    if confirmation != expected:
        raise ValueError(f"Explicit confirmation required: {expected}")
    if (case_dir / "receipt.json").exists() or (case_dir / "attempt.json").exists():
        raise ValueError("Publication receipt/attempt exists; reconcile before retrying")
    _, body, review, errors = check_case(case_dir, campaign, repo=repo, channel=channel)
    if errors:
        raise ValueError("Publication blocked: " + "; ".join(errors))
    executable = shutil.which("gh")
    if not executable:
        raise ValueError("GitHub CLI (gh) is not installed; install it and log in yourself")
    endpoint = f"repos/{repo}/issues" if channel == "public_issue" else (
        f"repos/{repo}/security-advisories/reports")
    title = review["approved_title"]
    payload = {"title": title, "body": body.decode("utf-8")}
    if channel == "private_security_report":
        payload = {"summary": title, "description": body.decode("utf-8")}
    attempt = {"repo": repo, "channel": channel,
               "body_sha256": digest(body), "started_at": datetime.now(timezone.utc).isoformat()}
    # A network timeout after GitHub accepted the POST is ambiguous: keep this
    # journal and require manual reconciliation, never retry automatically.
    with (case_dir / "attempt.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(attempt, indent=2) + "\n")
    result = subprocess.run([executable, "api", "--method", "POST", endpoint,
                             "--input", "-"], input=json.dumps(payload, ensure_ascii=False),
                            text=True, capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError("GitHub delivery failed or is uncertain; inspect the target manually. "
                           "The attempt journal blocks automatic retry.")
    answer = json.loads(result.stdout)
    url = answer.get("html_url")
    if not isinstance(url, str) or not url.startswith(f"https://github.com/{repo}/"):
        raise RuntimeError("GitHub response is ambiguous; inspect the target manually. "
                           "The attempt journal blocks automatic retry.")
    (case_dir / "receipt.json").write_text(json.dumps({**attempt, "url": url}, indent=2) + "\n", encoding="utf-8")
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description="Local developer issue drafts; no automatic publication")
    sub = parser.add_subparsers(dest="command", required=True)
    generate = sub.add_parser("generate", help="Create offline drafts from selected Confirmed decisions")
    generate.add_argument("--campaign", required=True, type=Path)
    generate.add_argument("--output", required=True, type=Path)
    check = sub.add_parser("check", help="Read-only readiness check for one case")
    check.add_argument("--campaign", required=True, type=Path)
    check.add_argument("--case", required=True, type=Path)
    check.add_argument("--repo", required=True)
    check.add_argument("--channel", required=True, choices=["public_issue", "private_security_report"])
    send = sub.add_parser("publish", help="Publish one separately reviewed case to GitHub")
    send.add_argument("--campaign", required=True, type=Path)
    send.add_argument("--case", required=True, type=Path)
    send.add_argument("--repo", required=True)
    send.add_argument("--channel", required=True, choices=["public_issue", "private_security_report"])
    send.add_argument("--confirm", required=True)
    args = parser.parse_args()
    try:
        if args.command == "generate":
            print(json.dumps(create_drafts(args.campaign, args.output), ensure_ascii=False))
        elif args.command == "check":
            _, _, _, errors = check_case(args.case, args.campaign, repo=args.repo,
                                         channel=args.channel)
            print(json.dumps({"publish_gate_passed": not errors, "errors": errors}, ensure_ascii=False))
            return 0 if not errors else 2
        else:
            print(publish(args.case, args.campaign, repo=args.repo,
                          channel=args.channel, confirmation=args.confirm))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
