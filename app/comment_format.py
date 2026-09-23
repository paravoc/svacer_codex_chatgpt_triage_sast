"""Plain-text presentation of Svacer comments; never rewrite their reasoning."""

from __future__ import annotations

import re


_INLINE_CODE = re.compile(r"(?<![\\`])(`+)(?!`)([^\r\n]*?)(?<!`)\1(?!`)")
_LINK = re.compile(r"(?<!!)\[([^\[\]\r\n]+)\]\(([^()\r\n]+)\)")
_LOCATION = re.compile(r"(?P<path>.+):(?P<start>[1-9][0-9]*)(?:[-–](?P<end>[1-9][0-9]*))?\Z")


def _source_label(match: re.Match[str]) -> str:
    label, target = match.groups()
    visible, destination = _LOCATION.fullmatch(label), _LOCATION.fullmatch(target)
    if not visible or not destination or "://" in target:
        return match.group(0)
    shown_path = visible["path"].replace("\\", "/")
    actual_path = destination["path"].replace("\\", "/")
    # Only remove a local source link when its label identifies the same file
    # and line(s). Keep unrelated/mismatched links, not a misleading short label.
    if (actual_path == shown_path or actual_path.endswith("/" + shown_path)) and (
        visible["start"], visible["end"]
    ) == (destination["start"], destination["end"]):
        return label
    return match.group(0)


def svacer_comment_text(value: str) -> str:
    """Remove inline-code wrappers and redundant local Markdown link targets.

    No translation, truncation, verdict selection or evidence edits happen here.
    Full source paths and quotations remain in the separate source_evidence field.
    """
    plain = _INLINE_CODE.sub(lambda m: m[2] if len(m[1]) == 1 else m[0], value)
    return _LINK.sub(_source_label, plain).strip()
