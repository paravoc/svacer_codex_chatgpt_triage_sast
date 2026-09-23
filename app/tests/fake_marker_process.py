"""Offline subprocess fixture. No model, authentication, network or real job data."""
import json
import os
import sys
import time
from pathlib import Path

directory = Path(sys.argv[1])
number = int((directory.parent if directory.name == "verification" else directory).name.split("-")[-1])
mode = sys.argv[2]
prompt = sys.stdin.read()
context = json.loads((directory / "context.json").read_text(encoding="utf-8"))
mid = context["batch"]["marker_ids"][0]
if mode in {"web", "web_sources"} and not context.get("source_request_round"):
    print(json.dumps({"type": "item.completed", "item": {"type": "web_search", "query": "nghttp2 v1.66.0",
        "action": {"type": "search", "queries": ["nghttp2 v1.66.0"]}}}), flush=True)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": mid}}), flush=True)
(directory / "started.json").write_text(json.dumps({"start": time.time(), "pid": os.getpid()}))
if mode.startswith("capacity") and number == 2 and (
        mode == "capacity_always" or not context.get("transport_retry_count")):
    # Even a plausible result file from a failed turn must not be accepted.
    Path(context["result_file"]).write_text(Path(f"fixture-result-{number}.json").read_text(encoding="utf-8"), encoding="utf-8")
    print(json.dumps({"type": "error", "message": "Selected model is at capacity. Please try a different model."}), flush=True)
    print(json.dumps({"type": "turn.failed", "error": {"message": "Selected model is at capacity. Please try a different model."}}), flush=True)
    sys.exit(1)
if mode == "stop":
    time.sleep(30)
if mode == "stall_second" and number == 2:
    time.sleep(30)
if mode in {"fail", "mixed"} and number == 2:
    sys.exit(7)
time.sleep((3 if mid == "m00" else .06) if mode.startswith("rolling") else (1.2 if number == 1 else 0.5))
if mode in {"sources", "web_sources"} and number == 2 and not context.get("source_request_round"):
    request = Path(context["source_request_file"])
    request.write_text(json.dumps([{"file_path": "include.h", "reason": "prove guard"}]))
else:
    (directory / "finished.json").write_text(json.dumps({"finish": time.time()}))
note = Path(context["result_file"])
if mode == "wrong_path":
    note = directory / "notes" / f"batch-{context['batch_number']:03d}-worker-{number}.json"
    note.parent.mkdir(exist_ok=True)
if mode == "repair_path" and not context.get("output_repair_count"):
    print(json.dumps({"type": "turn.completed"}), flush=True)
    sys.exit(0)
if mode.startswith("rolling"):
    data = json.loads(Path(f"fixture-marker-{mid}.json").read_text(encoding="utf-8"))
    if context.get("verification_only"):
        data = [{"marker_id": mid, "decision": "verified", "verifier_id": "offline-verifier",
                 "reason": "Independent proof", "evidence": ["same.go:1"], "rechecked_paths": ["entry -> sink"]}]
        if mode in {"rolling_verifier_shape", "rolling_verifier_shape_fails"}:
            if not context.get("verification_repair_count") or mode.endswith("_fails"):
                data[0]["evidence"] = [{"file": "same.go", "line": 1}]
            else:
                assert "evidence must be a non-empty string array" in prompt
                previous = Path(context["previous_result_file"])
                assert previous != note and previous.exists()
    elif mode == "rolling_incomplete" and mid == "m01":
        data = [{"marker_id": mid, "analysis_status": "needs_context", "verdict": "Unclear", "proof_gaps": ["missing guard"]}]
    note.write_text(json.dumps(data))
elif mode == "final_reply":
    Path(sys.argv[3]).write_text(json.dumps({"decisions": [{
        "marker_id": mid, "analysis_status": "needs_context", "proof_gaps": ["missing real evidence"]
    }], "source_requests": []}))
elif mode in {"mixed", "quality_repair", "quality_repair_fails", "evidence_numbered", "capacity_once", "capacity_always", "schema_enums"}:
    data = json.loads(Path(f"fixture-result-{number}.json").read_text(encoding="utf-8"))
    if mode == "schema_enums" and number == 2 and not context.get("worker_quality_repair_count"):
        data[0].update(severity="low", action="Please close the file")
    elif mode == "schema_enums" and number == 2:
        feedback = context.get("quality_feedback", [])
        # The repair process must actually receive the rejected enum contract.
        assert feedback and all(message in prompt for message in feedback)
        assert "Critical | Major | Minor" in prompt and "Fix required | Fix submitted | Ignore" in prompt
    if mode == "evidence_numbered":
        for ref in data[0]["source_evidence"]:
            ref["excerpt"] = "\n".join(f"{ref['line_start'] + offset}: {line}"
                                       for offset, line in enumerate(ref["excerpt"].splitlines()))
    if (mode == "quality_repair" and number == 2 and not context.get("worker_quality_repair_count")
            or mode == "quality_repair_fails" and number == 2):
        data[0]["component_defect_proven"] = True  # contradicts FP; must be repaired by this worker only
    note.write_text(json.dumps(data))
else:
    note.write_text(json.dumps([{"marker_id": mid, "analysis_status": "needs_context", "verdict": "Unclear",
                                 "proof_gaps": ["test-only missing guard"]}]))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": number * 10, "output_tokens": number}}), flush=True)
