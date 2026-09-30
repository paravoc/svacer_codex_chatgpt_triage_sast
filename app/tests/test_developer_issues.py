"""Offline tests: no GitHub account or network is used."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import developer_issues as issues


def fixture(tmp_path: Path) -> tuple[Path, Path, dict, dict]:
    root = tmp_path / "triage"
    job_dir = root / "RESULTS" / "job-1"
    job_dir.mkdir(parents=True)
    job = {"git_commit": "a" * 40, "git_ref": "v1.0", "snapshot_id": "snapshot-1",
           "repository_url": "https://github.com/acme/widget.git"}
    row = {"marker_id": "marker-1", "verdict": "Confirmed", "file": "/app/server/x.go",
           "line": 42, "warnClass": "DEREF_AFTER_NULL", "source_revision": "a" * 40,
           "verification": {"status": "verified"}, "source": "input", "sink": "x.go:42",
           "control": "missing guard", "product_reachability": "admin-only endpoint",
           "impact": "process stops", "review_contract_version": 1,
           "comment": "At x.go:42 the missing guard permits a process exit.",
           "source_evidence": [{"file_path": "/app/server/x.go", "line_start": 42,
                                "line_end": 42, "excerpt": "use(p)",
                                "roles": ["source", "sink", "control", "product_reachability"],
                                "supports": "The test source reaches the sink."}]}
    repo = job_dir / "repository"
    (repo / "server").mkdir(parents=True)
    (repo / "server" / "x.go").write_text("\n" * 41 + "use(p)\n", encoding="utf-8")
    context_dir = job_dir / "worker-runs" / "run-1" / "batch-001" / "worker-1"
    context_dir.mkdir(parents=True)
    context = {"batch": {"marker_ids": ["marker-1"]}, "revision": job["git_commit"],
               "snapshot_id": job["snapshot_id"], "repository": str(repo)}
    (context_dir / "context.json").write_text(json.dumps(context), encoding="utf-8")
    (job_dir / "job.json").write_text(json.dumps(job), encoding="utf-8")
    (job_dir / "decisions.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
    campaign = root / "RESULTS" / "campaign.json"
    campaign.write_text(json.dumps({"root": str(root), "jobs": [
        {"job": "job-1", "marker_ids": ["marker-1"]}]}), encoding="utf-8")
    return campaign, root / "RESULTS" / "drafts", job, row


def reviewed_case(case_dir: Path, job: dict, row: dict, *, kind="ordinary_bug",
                  channel="public_issue") -> dict:
    body = "# Reproducible bug\n\nThe admin endpoint exits unexpectedly.\n\n```text\nsafe test input\n```\n"
    (case_dir / "body.md").write_bytes(body.encode("utf-8"))
    (case_dir / "poc").mkdir()
    (case_dir / "poc" / "case.txt").write_text("safe test input", encoding="utf-8")
    review = {"target_repo": "acme/widget", "channel": channel,
              "finding_kind": kind, "reviewer": "human-reviewer",
              "approved_title": "Admin endpoint exits unexpectedly",
              "body_sha256": issues.digest(body.encode()),
              "decision_sha256": issues.digest(issues.canonical(row)),
              **{key: True for key in issues.REQUIRED_CHECKS},
              "reproduction": {
                  "source_revision": job["git_commit"], "harness_revision": "not applicable",
                  "environment": "Linux x86_64", "baseline_artifact": "baseline-sha256",
                  "control_artifact": "patched-sha256",
                  "command": "run isolated test", "baseline_observed": "exit 1",
                  "control_observed": "exit 0", "poc_file": "poc/case.txt",
                  "poc_sha256": issues.digest(b"safe test input"),
              },
              "fix": {"patch_file": "", "patch_sha256": "",
                      "no_patch_reason": "fix pending upstream", "regression_test": "test admin endpoint"}}
    (case_dir / "review.json").write_text(json.dumps(review), encoding="utf-8")
    return review


class DeveloperIssueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.campaign, self.drafts, self.job, self.row = fixture(Path(self.tmp.name))

    def case(self) -> Path:
        issues.create_drafts(self.campaign, self.drafts)
        return next(self.drafts.iterdir())

    def test_generate_is_offline_and_preserves_edits(self):
        case_dir = self.case()
        self.assertIn("LOCAL DRAFT", (case_dir / "body.md").read_text(encoding="utf-8"))
        self.assertIn("Offline PoC investigation task", (case_dir / "poc-task.md").read_text(encoding="utf-8"))
        self.assertFalse((case_dir / "review.json").exists())
        template = json.loads((case_dir / "review.template.v3.json").read_text())
        self.assertIs(template["disclosure_approved"], False)
        self.assertEqual(issues.create_drafts(self.campaign, self.drafts)["confirmed_drafts"], 1)
        (case_dir / "body.md").write_text("analyst edit", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not overwriting"):
            issues.create_drafts(self.campaign, self.drafts)

    def test_old_verified_cannot_publish(self):
        case_dir = self.case()
        with self.assertRaisesRegex(ValueError, "human review.json is missing"):
            issues.publish(case_dir, self.campaign, repo="acme/widget", channel="public_issue",
                           confirmation="PUBLISH marker-1 TO acme/widget AS public_issue")
        self.assertFalse((case_dir / "attempt.json").exists())

    def test_public_security_report_is_blocked(self):
        case_dir = self.case()
        review = reviewed_case(case_dir, self.job, self.row, kind="security_vulnerability")
        errors = issues.validate_review(case_dir, review, issues.read_json(case_dir / "case.json"),
                                        (case_dir / "body.md").read_bytes(), self.row, self.job,
                                        repo="acme/widget", channel="public_issue")
        self.assertTrue(any("cannot be published" in error for error in errors))

    def test_wrong_revision_and_changed_poc_are_blocked(self):
        case_dir = self.case()
        review = reviewed_case(case_dir, self.job, self.row)
        review["reproduction"]["source_revision"] = "b" * 40
        (case_dir / "poc" / "case.txt").write_text("changed", encoding="utf-8")
        errors = issues.validate_review(case_dir, review, issues.read_json(case_dir / "case.json"),
                                        (case_dir / "body.md").read_bytes(), self.row, self.job,
                                        repo="acme/widget", channel="public_issue")
        self.assertTrue(any("PoC source revision differs" in error for error in errors))
        self.assertTrue(any("PoC changed" in error for error in errors))

    def test_internal_hostname_is_blocked(self):
        case_dir = self.case()
        review = reviewed_case(case_dir, self.job, self.row)
        body = b"# Bug\n\nSee svacer01.internal for details.\n"
        (case_dir / "body.md").write_bytes(body)
        review["body_sha256"] = issues.digest(body)
        errors = issues.validate_review(case_dir, review, issues.read_json(case_dir / "case.json"),
                                        body, self.row, self.job,
                                        repo="acme/widget", channel="public_issue")
        self.assertTrue(any("internal hostnames" in error for error in errors))

    def test_fabricated_source_evidence_blocks_publication(self):
        case_dir = self.case()
        reviewed_case(case_dir, self.job, self.row)
        job_dir = self.campaign.parent / "job-1"
        rows = issues.decisions_by_id(job_dir / "decisions.jsonl")
        rows["marker-1"]["source_evidence"][0]["line_start"] = 99999
        (job_dir / "decisions.jsonl").write_text(json.dumps(rows["marker-1"]) + "\n", encoding="utf-8")
        _, _, _, errors = issues.check_case(case_dir, self.campaign, repo="acme/widget",
                                             channel="public_issue")
        self.assertTrue(any("source contract failed" in error for error in errors))

    def test_private_security_report_uses_private_endpoint(self):
        case_dir = self.case()
        reviewed_case(case_dir, self.job, self.row, kind="security_vulnerability",
                      channel="private_security_report")
        response = SimpleNamespace(returncode=0, stdout=json.dumps(
            {"html_url": "https://github.com/acme/widget/security/advisories/GHSA-test"}))
        with patch.object(issues.shutil, "which", return_value="gh"), patch.object(
                issues.subprocess, "run", return_value=response) as run:
            issues.publish(case_dir, self.campaign, repo="acme/widget",
                           channel="private_security_report",
                           confirmation="PUBLISH marker-1 TO acme/widget AS private_security_report")
        self.assertIn("security-advisories/reports", run.call_args.args[0][4])

    def test_publish_journals_one_network_attempt(self):
        case_dir = self.case()
        reviewed_case(case_dir, self.job, self.row)
        response = SimpleNamespace(returncode=0, stdout=json.dumps(
            {"html_url": "https://github.com/acme/widget/issues/17"}))
        with patch.object(issues.shutil, "which", return_value="gh"), patch.object(
                issues.subprocess, "run", return_value=response) as run:
            url = issues.publish(case_dir, self.campaign, repo="acme/widget", channel="public_issue",
                                 confirmation="PUBLISH marker-1 TO acme/widget AS public_issue")
        self.assertTrue(url.endswith("/issues/17"))
        self.assertEqual(run.call_args.args[0][-2:], ["--input", "-"])
        self.assertTrue((case_dir / "receipt.json").exists())
        with self.assertRaisesRegex(ValueError, "receipt/attempt"):
            issues.publish(case_dir, self.campaign, repo="acme/widget", channel="public_issue",
                           confirmation="PUBLISH marker-1 TO acme/widget AS public_issue")


if __name__ == "__main__":
    unittest.main()
