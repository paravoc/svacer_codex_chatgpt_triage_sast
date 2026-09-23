"""Accept a fresh final JSON reply without requiring model-side file writes.

Only transports the assigned IDs. Decision/evidence validation remains mandatory.
No extraction of JSON fragments from prose, logs or search results.
"""
import json
from pathlib import Path


def read_final_reply(path: Path, marker_ids: list[str]) -> tuple[list[dict], list[dict]] | None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 512 * 1024:
        return None
    text = path.read_text(encoding="utf-8-sig").strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    try:
        value = json.loads(text)
    except ValueError:
        return None
    requests = []
    if isinstance(value, dict) and "decisions" in value:
        requests = value.get("source_requests", [])
        value = value["decisions"]
    elif isinstance(value, dict):
        value = [value]
    if (not isinstance(value, list) or len(value) != len(marker_ids)
            or any(not isinstance(row, dict) for row in value)
            or sorted(str(row.get("marker_id", "")) for row in value) != sorted(marker_ids)
            or not isinstance(requests, list) or len(requests) > 10
            or any(not isinstance(item, dict) for item in requests)):
        return None
    return value, requests
