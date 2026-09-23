"""Pinned Envoy dependency sources: bounded downloads, no build-code execution.

Snapshot previews remain authoritative. These trees provide additional source and
build recipes, not generated configuration or proof of product reachability.
"""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path, PurePosixPath

from decision_quality import safe_source_path
from triage_queue import atomic_write_json

LEGACY_NAMES = {"com_github_luajit_luajit", "com_github_libevent_libevent", "bazel_gazelle"}
HOSTS = {"github.com", "codeload.github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_CONTENT = 256 * 1024 * 1024
MAX_FILE = 8 * 1024 * 1024


def cache_io_path(path: Path) -> Path:
    """Use long-path Windows I/O without changing logical evidence identities.

    Containment checks and manifests always use normal resolved paths. Only
    the final I/O uses the extended spelling, so deep upstream source trees
    do not depend on the machine's LongPathsEnabled registry setting.
    """
    if os.name != "nt":
        return path
    absolute = str(path.resolve())
    if absolute.startswith("\\\\?\\"):
        return Path(absolute)
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def member_path(value: str) -> str:
    value = value.removeprefix("./")
    parts = PurePosixPath(value).parts
    if (not parts or value.startswith("/") or "\\" in value or ":" in value
            or any(part in {"..", "."} for part in parts) or not safe_source_path(value)):
        raise ValueError("Unsafe dependency path")
    return "/".join(parts)


def dependency_name(path: str) -> str | None:
    parts = path.replace("\\", "/").split("/")
    # Bazel's external repository segment identifies the dependency, not a
    # hardcoded list of three libraries. Unknown recipes fail explicitly below.
    for index, part in enumerate(parts[:-2]):
        if part in {"external", "dependency-sources"}:
            name = parts[index + 1]
            if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.+~-]*", name):
                return name
    # Older foreign_cc previews sometimes lack the external/ prefix.
    return next((name for name in parts if name in LEGACY_NAMES), None)


def repository_bytes(repository: Path, relative: str) -> bytes:
    target = (repository / member_path(relative)).resolve()
    if not target.is_relative_to(repository.resolve()):
        raise ValueError("Dependency recipe/patch escaped the repository")
    return target.read_bytes()


def pinned_spec(repository: Path, name: str) -> dict:
    """Read literal lock fields; never execute Starlark/Python from the project."""
    tree = ast.parse(repository_bytes(repository, "bazel/repository_locations.bzl").decode("utf-8"))
    top = next(node.value for node in tree.body if isinstance(node, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "REPOSITORY_LOCATIONS_SPEC" for t in node.targets))
    node = next((k.value for k in top.keywords if k.arg == name), None)
    if node is None:
        raise ValueError("No pinned dependency recipe for " + name)
    wanted = {"version", "sha256", "urls", "strip_prefix"}
    fields = {k.arg: ast.literal_eval(k.value) for k in node.keywords if k.arg in wanted}
    if not re.fullmatch(r"[0-9a-f]{64}", fields.get("sha256", "")) or not fields.get("version"):
        raise ValueError("Dependency is not checksum-pinned")
    fields["urls"] = [url.format(version=fields["version"]) for url in fields["urls"]]
    fields["strip_prefix"] = fields.get("strip_prefix", "").format(version=fields["version"])
    recipe = ast.parse(repository_bytes(repository, "bazel/repositories.bzl").decode("utf-8"))
    calls = [n for n in ast.walk(recipe) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "external_http_archive" and
             ((n.args and isinstance(n.args[0], ast.Constant) and n.args[0].value == name)
              or any(k.arg == "name" and isinstance(k.value, ast.Constant) and k.value.value == name for k in n.keywords))]
    if len(calls) != 1:
        raise ValueError("Dependency build recipe is ambiguous")
    kwargs = {k.arg: k.value for k in calls[0].keywords}
    if any(k in kwargs for k in ("urls", "url", "sha256", "strip_prefix", "add_prefix", "type")):
        raise ValueError("Dependency archive overrides need manual preparation")
    if any(k in kwargs for k in ("patch_cmds", "patch_cmds_win", "patch_tool")):
        raise ValueError("Custom dependency transformations need manual preparation")
    patches = ast.literal_eval(kwargs["patches"]) if "patches" in kwargs else []
    if patches and ast.literal_eval(kwargs.get("patch_args", ast.parse("[]", mode="eval").body)) != ["-p1"]:
        raise ValueError("Unsupported dependency patch layout")
    fields["patches"] = []
    for label in patches:
        if not label.startswith("@envoy//") or ":" not in label:
            raise ValueError("Patch is not in the pinned repository")
        relative = member_path(label.removeprefix("@envoy//").replace(":", "/", 1))
        data = repository_bytes(repository, relative)
        fields["patches"].append({"path": relative, "sha256": digest(data)})
    recipes = ["bazel/repository_locations.bzl", "bazel/repositories.bzl"]
    recipes.extend(patch["path"] for patch in fields["patches"])
    if "build_file" in kwargs:
        label = ast.literal_eval(kwargs["build_file"])
        if not isinstance(label, str) or not label.startswith("@envoy//") or ":" not in label:
            raise ValueError("Build file is not in the pinned repository")
        recipes.append(member_path(label.removeprefix("@envoy//").replace(":", "/", 1)))
    foreign_build = repository / "bazel/foreign_cc/BUILD"
    if foreign_build.is_file() and name in repository_bytes(repository, "bazel/foreign_cc/BUILD").decode("utf-8"):
        recipes.append("bazel/foreign_cc/BUILD")
    fields["product_recipes"] = list(dict.fromkeys(recipes))
    return fields


def checked_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in HOSTS or parsed.username or parsed.password
            or parsed.port not in (None, 443)):
        raise ValueError("Unsupported dependency download origin")


class RestrictedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        checked_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str) -> bytes:
    checked_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": "Svacer-Triage-source-cache"})
    started, chunks, size = time.monotonic(), [], 0
    with urllib.request.build_opener(RestrictedRedirect()).open(request, timeout=20) as reply:
        while True:
            if time.monotonic() - started > 90:
                raise TimeoutError("Dependency download deadline exceeded")
            chunk = reply.read(256 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_ARCHIVE:
                raise ValueError("Dependency archive exceeds size limit")
            chunks.append(chunk)
    return b"".join(chunks)


def unpack(data: bytes, strip_prefix: str) -> dict[str, bytes]:
    """Never extract links, permissions, devices or paths onto the filesystem."""
    result: dict[str, bytes] = {}
    seen = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for index, entry in enumerate(archive):
            if index > 20000:
                raise ValueError("Too many archive entries")
            if not entry.isfile():
                continue
            # Test credentials/key fixtures are not needed for code triage.
            # Reject traversal separately; skip sensitive names without reading them.
            if ".." not in entry.name.replace("\\", "/").split("/") and not safe_source_path(entry.name):
                continue
            path = member_path(entry.name)
            if strip_prefix:
                prefix = member_path(strip_prefix) + "/"
                if not path.startswith(prefix):
                    raise ValueError("Archive strip prefix mismatch")
                path = member_path(path[len(prefix):])
            if entry.size < 0 or entry.size > MAX_FILE:
                raise ValueError("Dependency file exceeds size limit")
            total += entry.size
            if total > MAX_CONTENT or path.casefold() in seen or path == "source-manifest.json":
                raise ValueError("Oversized archive or duplicate path")
            seen.add(path.casefold())
            with archive.extractfile(entry) as source:
                result[path] = source.read(MAX_FILE + 1)
    return result


def apply_patch_data(files: dict[str, bytes], patch: str) -> None:
    """Unified text patch, exact full context only; no fuzz or commands.

    Old patches can have stale line numbers. Relocation is allowed only for a
    unique exact old-side block; never discard context lines to make it apply.
    """
    # Git's Windows checkout may convert the patch file itself to CRLF. Those
    # transport line endings must not be inserted into the Linux source tree.
    lines = patch.replace("\r\n", "\n").splitlines(keepends=True)
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            if lines[index].startswith(("GIT binary patch", "rename from", "copy from")):
                raise ValueError("Unsupported patch operation")
            index += 1
            continue
        old = lines[index][4:].split("\t", 1)[0].strip()
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise ValueError("Invalid patch header")
        new = lines[index][4:].split("\t", 1)[0].strip()
        if not new.startswith("b/") or (old != "/dev/null" and old != "a/" + new[2:]):
            raise ValueError("Patch changes file identity")
        path = member_path(new[2:])
        original = [] if old == "/dev/null" else files[path].decode("utf-8").splitlines(keepends=True)
        output, cursor = [], 0
        index += 1
        while index < len(lines) and lines[index].startswith("@@ "):
            hunk = re.match(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", lines[index])
            if not hunk:
                raise ValueError("Invalid patch hunk")
            start = max(0, int(hunk[1]) - 1)
            removed, added, operations = 0, 0, []
            index += 1
            while index < len(lines) and (removed < int(hunk[2] or 1) or added < int(hunk[4] or 1)):
                # Unified diffs may omit the space on an empty context line.
                op, text = (" ", "\n") if lines[index] == "\n" else (lines[index][0], lines[index][1:])
                if op not in {" ", "+", "-"}:
                    raise ValueError("Invalid patch line")
                if op in {" ", "-"}:
                    removed += 1
                if op in {" ", "+"}:
                    added += 1
                operations.append((op, text))
                index += 1
            if removed != int(hunk[2] or 1) or added != int(hunk[4] or 1):
                raise ValueError("Patch hunk length mismatch")
            expected = [text.rstrip("\r\n") for op, text in operations if op in {" ", "-"}]
            def matches(position):
                return (cursor <= position <= len(original) - len(expected)
                        and [line.rstrip("\r\n") for line in original[position:position + len(expected)]] == expected)
            if not matches(start):
                positions = [pos for pos in range(cursor, len(original) - len(expected) + 1)
                             if expected and matches(pos)]
                if len(positions) != 1:
                    raise ValueError("Patch context mismatch or ambiguous relocation")
                start = positions[0]
            output.extend(original[cursor:start])
            output.extend(text for op, text in operations if op in {" ", "+"})
            cursor = start + removed
        output.extend(original[cursor:])
        files[path] = "".join(output).encode("utf-8")


def prepare_dependency(job: Path, context: dict, name: str, *, allow_download=True) -> dict:
    repository = Path(context["repository"])
    spec = pinned_spec(repository, name)
    identity = {"spec": {key: value for key, value in spec.items() if key != "product_recipes"},
                "revision": context["revision"]}
    recipe_paths = [str(repository / path) for path in spec["product_recipes"]]
    if spec["patches"]:
        identity["patch_engine"] = "unified-lf-v1"
    key = digest(json.dumps(identity, sort_keys=True).encode())
    parent = job / "dependency-sources" / name
    parent.mkdir(parents=True, exist_ok=True)
    record_path = parent / f"{key}.json"
    if record_path.is_file():
        record = json.loads(record_path.read_text(encoding="utf-8"))
        manifest = job / record["manifest_path"]
        if manifest.resolve().is_relative_to(parent.resolve()) and digest(manifest.read_bytes()) == record["manifest_sha256"]:
            return {**record, "product_recipe_paths": recipe_paths}
    if not allow_download:
        raise ValueError("No verified cached archive; offline preparation cannot download")
    data = download(spec["urls"][0])
    if digest(data) != spec["sha256"]:
        raise ValueError("Dependency SHA-256 mismatch")
    files = unpack(data, spec["strip_prefix"])
    for patch in spec["patches"]:
        raw = repository_bytes(repository, patch["path"])
        if digest(raw) != patch["sha256"]:
            raise ValueError("Patch changed during preparation")
        apply_patch_data(files, raw.decode("utf-8"))
    # A failed preparation is never reused. No existing source tree is overwritten.
    directory = Path(tempfile.mkdtemp(prefix=key[:12] + "-", dir=parent))
    manifest = {}
    for path, content in files.items():
        target = directory / member_path(path)
        cache_io_path(target.parent).mkdir(parents=True, exist_ok=True)
        cache_io_path(target).write_bytes(content)
        manifest[path] = digest(content)
    manifest_path = directory / "source-manifest.json"
    atomic_write_json(manifest_path, manifest)
    record = {"name": name, "version": spec["version"], "archive_sha256": spec["sha256"],
              "product_revision": context["revision"], "patches": spec["patches"],
              "root": directory.relative_to(job).as_posix(), "files": len(files),
              "manifest_path": manifest_path.relative_to(job).as_posix(),
              "manifest_sha256": digest(manifest_path.read_bytes()),
              "product_recipe_paths": recipe_paths,
              "provenance": "pinned_dependency_archive_with_product_patches; not generated build configuration"}
    atomic_write_json(record_path, record)
    return record


def check_snapshot_anchors(job: Path, context: dict, record: dict) -> dict:
    """Tie the archive to this snapshot as well as the product lock/patches."""
    checked, conflicts = [], []
    temporary = {**context, "dependency_sources": [record]}
    for source in [*context.get("external_sources", []), *context.get("source_catalog", [])]:
        path = source.get("file_path", "")
        if dependency_name(path) != record["name"] or path in checked or path in conflicts:
            continue
        local = (job / source["local_path"]).resolve()
        if not local.is_relative_to((job / "external-sources").resolve()):
            raise ValueError("Snapshot preview escaped its root")
        saved = json.loads(local.read_text(encoding="utf-8-sig"))
        if saved.get("snapshot_id") != context.get("snapshot_id") or saved.get("file_path") != path:
            continue
        preview = saved.get("preview", {})
        content, total = preview.get("content"), preview.get("total_lines")
        if (not isinstance(content, str) or type(total) is not int or total < 1
                or preview.get("line") not in (0, 1) or len(content.split("\n")) < total):
            continue
        archived = dependency_text(job, temporary, path, require_anchor=False)
        if archived is not None:
            (checked if archived.splitlines() == content.splitlines() else conflicts).append(path)
    return {**record, "snapshot_id": context.get("snapshot_id"),
            "snapshot_matches": checked, "snapshot_conflicts": conflicts}


def prepare_dependencies(job: Path, context: dict, progress=None, *, allow_download=True) -> dict:
    paths = [row.get("file_path", "") for row in context.get("external_sources", [])]
    for assignment in context.get("batch", {}).get("assignments", []):
        for field in ("markers", "decisions"):
            paths.extend(row.get("file", "") for row in assignment.get(field, []) if isinstance(row, dict))
    names = sorted({name for path in paths if (name := dependency_name(path))})
    if not context.get("repository") or not (Path(context["repository"]) / "bazel/repository_locations.bzl").is_file():
        return context
    prepared = {row["name"]: row for row in context.get("dependency_sources", [])}
    errors = []
    for name in names:
        if progress:
            progress(name)
        try:
            prepared[name] = check_snapshot_anchors(job, context, prepare_dependency(job, context, name, allow_download=allow_download))
            if prepared[name]["snapshot_conflicts"]:
                errors.append({"name": name, "error": "Source differs from snapshot; archive evidence disabled"})
            elif not prepared[name]["snapshot_matches"]:
                errors.append({"name": name, "error": "No matching snapshot source; archive evidence disabled"})
        except Exception as exc:
            prepared.pop(name, None)
            errors.append({"name": name, "error": type(exc).__name__ + ": " + str(exc)[:160]})
    context.update(dependency_sources=list(prepared.values()), dependency_source_errors=errors)
    return context


def dependency_text(job: Path, context: dict, file_path: str, *, require_anchor=True) -> str | None:
    name = dependency_name(file_path)
    if not name or not safe_source_path(file_path):
        return None
    records = [row for row in context.get("dependency_sources", []) if row.get("name") == name]
    supplied = Path(file_path)
    # Worker-relative cache paths are rooted at the job, never at the runner's
    # current directory (which is usually app/ during final validation).
    candidate = (supplied if supplied.is_absolute() else job / supplied).resolve()
    # Parallel source rounds can prepare equivalent immutable caches in
    # different directories. Honor the cache actually cited by this worker.
    record = next((row for row in records if candidate.is_relative_to((job / row["root"]).resolve())),
                  records[0] if records else None)
    if not record or record.get("product_revision") != context.get("revision"):
        return None
    if require_anchor and (record.get("snapshot_id") != context.get("snapshot_id")
                           or not record.get("snapshot_matches") or record.get("snapshot_conflicts")):
        return None
    root = (job / record["root"]).resolve()
    normalized = file_path.replace("\\", "/")
    if candidate.is_relative_to(root):
        relative = candidate.relative_to(root).as_posix()
    elif name + "/" in normalized:
        relative = member_path(normalized.split(name + "/", 1)[1])
    else:
        return None
    manifest = (job / record["manifest_path"]).resolve()
    if not root.is_relative_to((job / "dependency-sources").resolve()) or not manifest.is_relative_to(root):
        raise ValueError("Dependency cache path escaped its root")
    raw = manifest.read_bytes()
    if digest(raw) != record["manifest_sha256"]:
        raise ValueError("Dependency manifest changed")
    expected = json.loads(raw).get(relative)
    if not expected:
        return None
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Dependency file escaped its root")
    data = cache_io_path(target).read_bytes()
    if digest(data) != expected:
        raise ValueError("Dependency source changed")
    return data.decode("utf-8-sig")


def main() -> int:
    """Explicit offline-model preflight; never change decisions, queue or old contexts."""
    import argparse
    import subprocess
    from codex_run import source_catalog, hidden_subprocess_kwargs
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", required=True, type=Path)
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--offline", action="store_true", help="Verify/reuse existing caches without downloading")
    parser.add_argument("--check-path", action="append", default=[])
    args = parser.parse_args()
    job, context_path = args.job.resolve(), args.context.resolve()
    if not context_path.is_relative_to(job) or context_path.is_symlink():
        raise ValueError("Context must belong to the selected job")
    context = json.loads(context_path.read_text(encoding="utf-8"))
    repository = Path(context["repository"]).resolve()
    if repository != job / "repository":
        raise ValueError("Repository must belong to this job")
    def git(*command):
        return subprocess.check_output(["git", "-C", str(repository), *command], text=True,
                                       **hidden_subprocess_kwargs()).strip()
    if git("rev-parse", "HEAD") != context["revision"] or git("status", "--porcelain", "--untracked-files=no"):
        raise ValueError("Source checkout revision or tracked files changed")
    context["source_catalog"] = source_catalog(job, context["snapshot_id"])
    prepare_dependencies(job, context, lambda name: print("Preparing " + name, flush=True), allow_download=not args.offline)
    directory = job / "dependency-sources"
    directory.mkdir(exist_ok=True)
    atomic_write_json(directory / "preparation.json", context)
    for record in context.get("dependency_sources", []):
        print(json.dumps({"name": record["name"], "version": record["version"], "files": record["files"],
                          "snapshot_matches": len(record["snapshot_matches"]), "snapshot_conflicts": len(record["snapshot_conflicts"])}))
    from decision_quality import source_text
    for path in args.check_path:
        text = source_text(job, context, path)
        print(json.dumps({"checked_path": path, "lines": len(text.splitlines())}))
    print(json.dumps({"errors": context.get("dependency_source_errors", [])}))
    return 1 if context.get("dependency_source_errors") else 0


if __name__ == "__main__":
    raise SystemExit(main())
