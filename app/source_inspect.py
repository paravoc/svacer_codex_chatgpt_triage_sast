"""Bounded local source navigation for workers; no network, rg or shell dependency.

Search only the pinned product's tracked files, verified dependency manifests and
this snapshot's source catalog. Failed/truncated searches are never proof of absence.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import time
from pathlib import Path

from decision_quality import repository_source, safe_source_path, source_text
from dependency_sources import dependency_name, digest, member_path

MAX_SCAN_BYTES = 64 * 1024 * 1024
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 48 * 1024
MAX_SEARCH_SECONDS = 20


class SourceIndex:
    def __init__(self, job: Path, context: dict):
        self.job, self.context = job.resolve(), context
        self.repository = Path(context["repository"]).resolve()
        if self.repository != self.job / "repository":
            raise ValueError("Source repository must belong to this job")
        self.paths: dict[str, str] = {}
        self.errors = []
        def git(*args):
            result = subprocess.run(["git", "-C", str(self.repository), *args],
                                    capture_output=True, timeout=20,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode:
                raise ValueError("Cannot enumerate the pinned product repository")
            return result.stdout.decode("utf-8")
        if git("rev-parse", "HEAD").strip() != context["revision"]:
            raise ValueError("Product checkout is not at the context revision")
        for path in git("ls-files", "-z").split("\0"):
            if path and safe_source_path(path):
                self.paths[path] = "product"
        self.tracked = set(self.paths)
        for record in context.get("dependency_sources", []):
            if (record.get("product_revision") != context["revision"]
                    or record.get("snapshot_id") != context.get("snapshot_id")
                    or not record.get("snapshot_matches") or record.get("snapshot_conflicts")):
                continue
            try:
                root = (self.job / record["root"]).resolve()
                manifest = (self.job / record["manifest_path"]).resolve()
                if (not root.is_relative_to(self.job / "dependency-sources")
                        or not manifest.is_relative_to(root)):
                    raise ValueError("Dependency escaped cache")
                raw = manifest.read_bytes()
                if digest(raw) != record["manifest_sha256"]:
                    raise ValueError("Dependency manifest changed")
                for path in json.loads(raw):
                    # Stable Bazel identities survive independently prepared
                    # worker caches; private temporary cache roots do not.
                    self.paths[f"external/{record['name']}/{member_path(path)}"] = "dependency"
            except (OSError, ValueError, KeyError) as exc:
                self.errors.append(f"Dependency {record.get('name')}: {type(exc).__name__}")
        for record in [*context.get("source_catalog", []), *context.get("external_sources", [])]:
            path = record.get("file_path", "")
            if safe_source_path(path):
                self.paths[path] = "snapshot"

    def content(self, path: str) -> str:
        if not safe_source_path(path):
            raise ValueError("Unsafe source path")
        if self.paths.get(path) != "snapshot":
            local = repository_source(self.repository, path)
            if local is not None and local.relative_to(self.repository).as_posix() not in self.tracked:
                raise ValueError("Untracked product file is not source evidence")
            if local is not None and local.stat().st_size > MAX_SOURCE_BYTES:
                raise ValueError("Source file exceeds size limit")
        text = source_text(self.job, self.context, path)
        if len(text.encode("utf-8")) > MAX_SOURCE_BYTES or "\0" in text:
            raise ValueError("Source is too large or binary")
        return text

    def candidates(self, scope: str, pattern: str):
        pattern = pattern.replace("\\", "/")
        for path, origin in self.paths.items():
            normalized = path.replace("\\", "/")
            matches = (fnmatch.fnmatchcase(normalized, pattern) or
                       ("/" not in pattern and fnmatch.fnmatchcase(normalized.rsplit("/", 1)[-1], pattern)))
            # "dependency" describes the code's owner, not its storage format.
            # A header fetched from Svacer is still dependency code even when
            # archive preparation failed. Keep "snapshot" as its provenance.
            in_scope = (scope in {"all", origin}
                        or (scope == "dependency" and origin == "snapshot"
                            and dependency_name(path) is not None))
            if in_scope and matches:
                yield path, origin

    def files(self, scope="all", pattern="*", limit=60):
        found = list(self.candidates(scope, pattern))
        return {"files": [{"file_path": path, "scope": origin} for path, origin in found[:limit]],
                "total": len(found), "truncated": len(found) > limit, "errors": self.errors}

    def search(self, term: str, scope="all", pattern="*", limit=40):
        if not term or len(term) > 300 or "\n" in term:
            raise ValueError("Search requires a nonempty single-line literal, at most 300 characters")
        rows, errors = [], list(self.errors)
        scanned = size = output_size = 0
        truncated = False
        started = time.monotonic()
        candidates = list(self.candidates(scope, pattern))
        for path, origin in candidates:
            if size >= MAX_SCAN_BYTES or time.monotonic() - started >= MAX_SEARCH_SECONDS:
                truncated = True
                break
            try:
                content = self.content(path)
            except (OSError, ValueError, KeyError) as exc:
                errors.append(f"{path}: {type(exc).__name__}")
                continue
            scanned += 1
            size += len(content.encode("utf-8"))
            for number, line in enumerate(content.splitlines(), 1):
                if term not in line:
                    continue
                row = {"file_path": path, "line": number, "text": line[:500],
                       "line_truncated": len(line) > 500, "scope": origin}
                row_size = len(json.dumps(row).encode())
                if len(rows) >= limit or output_size + row_size > MAX_OUTPUT_BYTES:
                    truncated = True
                    break
                rows.append(row)
                output_size += row_size
            if truncated:
                break
        return {"matches": rows, "files_scanned": scanned, "truncated": truncated,
                "complete": bool(candidates) and not truncated and not errors, "errors": errors[:10],
                "files_matched": len(candidates),
                "error_count": len(errors),
                "notice": ("No matches is not proof of unreachability. Check scope and complete/truncated."
                           if candidates else "No files matched scope/glob. List files or correct the glob; no source was searched.")}

    def read(self, path: str, start: int, end: int, *, evidence=False):
        lines = self.content(path).splitlines()
        requested_end = end
        if not 1 <= start <= end or start > len(lines):
            raise ValueError("Choose an existing range of at most 99 lines")
        if evidence and end - start >= 99:
            raise ValueError("Evidence range must have at most 99 lines")
        if evidence and end > len(lines):
            raise ValueError(f"Evidence range exceeds EOF ({len(lines)} lines)")
        end = min(end, len(lines), start + 98)
        excerpt = "\n".join(lines[start - 1:end])
        if len(excerpt.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ValueError("Requested excerpt too large; narrow the range")
        if evidence:
            return {"file_path": path, "line_start": start, "line_end": end, "excerpt": excerpt}
        return {"file_path": path, "total_lines": len(lines),
                "line_start": start, "line_end": end, "clamped_to_eof": requested_end > len(lines),
                "truncated": end < min(requested_end, len(lines)),
                "next_line": end + 1 if end < min(requested_end, len(lines)) else None,
                "lines": [f"{number}: {lines[number - 1]}" for number in range(start, end + 1)]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("operation", choices=["files", "search", "read", "evidence"])
    parser.add_argument("--scope", choices=["all", "product", "dependency", "snapshot"], default="all")
    parser.add_argument("--glob", default="*")
    parser.add_argument("--term", default="")
    parser.add_argument("--file", default="")
    parser.add_argument("--start", type=int, default=1)
    parser.add_argument("--end", type=int, default=40)
    parser.add_argument("--limit", type=int, default=40, choices=range(1, 101), metavar="1..100")
    args = parser.parse_args(argv)
    try:
        job, context_path = args.job.resolve(), args.context.resolve()
        if not context_path.is_relative_to(job):
            raise ValueError("Context must belong to this job")
        index = SourceIndex(job, json.loads(context_path.read_text(encoding="utf-8")))
        if args.operation == "files":
            result = index.files(args.scope, args.glob, args.limit)
        elif args.operation == "search":
            result = index.search(args.term, args.scope, args.glob, args.limit)
        else:
            result = index.read(args.file, args.start, args.end, evidence=args.operation == "evidence")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(json.dumps({"error": str(exc), "complete": False}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
