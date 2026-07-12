from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any

import yaml
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("vault-writer-mcp")

REMOTE_NAME = "naswebdav"
REMOTE_ROOT = os.getenv("WEBDAV_REMOTE_PATH", "").strip("/")
NO_CHECK_CERT = os.getenv("WEBDAV_NO_CHECK_CERTIFICATE", "false").lower() == "true"
ARCHIVE_ROOT = os.getenv("CURATOR_ARCHIVE_ROOT", ".curator-archive").strip("/") or ".curator-archive"
ALLOW_HARD_DELETE = os.getenv("CURATOR_ALLOW_HARD_DELETE", "false").lower() == "true"
SYNC_REQUEST_FILE = os.getenv("SYNC_REQUEST_FILE", "/control/request-sync")

mcp = FastMCP(
    "vault-writer-mcp",
    instructions=(
        "Writes Markdown notes directly to the source WebDAV vault through rclone. "
        "Prefer archive_note over delete_note. Use expected_sha256 on destructive edits "
        "to avoid clobbering concurrent changes."
    ),
    version="0.1.0",
)


class WriterError(ValueError):
    pass


@dataclass
class NoteState:
    content: str
    sha256: str
    size_bytes: int
    modified_at: str | None


def _remote_base() -> str:
    if REMOTE_ROOT:
        return f"{REMOTE_NAME}:{REMOTE_ROOT}"
    return f"{REMOTE_NAME}:"


def _join_remote(path: str | None = None) -> str:
    base = _remote_base()
    if not path:
        return base
    clean = path.lstrip("/")
    if base.endswith(":"):
        return f"{base}{clean}"
    return f"{base}/{clean}"


def _rclone(args: list[str], *, stdin_text: str | None = None) -> str:
    command = ["rclone"]
    if NO_CHECK_CERT:
        command.append("--no-check-certificate")
    command.extend(args)
    logger.info("Running rclone command: %s", command)
    proc = subprocess.run(
        command,
        input=stdin_text,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout).strip()
        raise WriterError(stderr or "rclone command failed")
    return proc.stdout


def _normalize_note_path(path: str) -> str:
    if not path:
        raise WriterError("path is required")
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise WriterError("path must be relative to the vault root")
    parts = [part for part in pure.parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise WriterError("path cannot traverse outside the vault")
    normalized = posixpath.normpath("/".join(parts))
    if normalized in ("", "."):
        raise WriterError("path must target a note")
    if not normalized.endswith(".md"):
        raise WriterError("only .md notes are supported by this writer")
    return normalized


def _normalize_folder_path(path: str = "") -> str:
    if not path:
        return ""
    pure = PurePosixPath(path)
    if pure.is_absolute():
        raise WriterError("folder path must be relative to the vault root")
    parts = [part for part in pure.parts if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise WriterError("folder path cannot traverse outside the vault")
    normalized = posixpath.normpath("/".join(parts))
    return "" if normalized == "." else normalized


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lsjson_stat(path: str) -> dict[str, Any] | None:
    try:
        output = _rclone(["lsjson", "--stat", _join_remote(path)])
    except WriterError as exc:
        message = str(exc).lower()
        if "not found" in message or "doesn't exist" in message or "object not found" in message:
            return None
        raise
    payload = json.loads(output)
    if isinstance(payload, list):
        return payload[0] if payload else None
    return payload


def _ensure_parent_folder(path: str) -> None:
    folder = posixpath.dirname(path)
    if folder and folder != ".":
        _rclone(["mkdir", _join_remote(folder)])


def _read_note_state(path: str) -> NoteState:
    metadata = _lsjson_stat(path)
    if metadata is None:
        raise WriterError(f"note not found: {path}")
    content = _rclone(["cat", _join_remote(path)])
    return NoteState(
        content=content,
        sha256=_sha256(content),
        size_bytes=len(content.encode("utf-8")),
        modified_at=metadata.get("ModTime"),
    )


def _assert_expected(path: str, expected_sha256: str | None) -> NoteState | None:
    metadata = _lsjson_stat(path)
    if metadata is None:
        if expected_sha256:
            raise WriterError(f"note does not exist but expected_sha256 was provided: {path}")
        return None
    state = _read_note_state(path)
    if expected_sha256 and state.sha256 != expected_sha256:
        raise WriterError(
            f"concurrency check failed for {path}: expected {expected_sha256}, current {state.sha256}"
        )
    return state


FRONTMATTER_RE = re.compile(r"\A---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw = match.group(1)
    parsed = yaml.safe_load(raw) or {}
    if not isinstance(parsed, dict):
        raise WriterError("existing frontmatter is not a YAML object")
    body = text[match.end() :]
    return parsed, body


def _render_note(frontmatter: dict[str, Any], body: str) -> str:
    dumped = yaml.safe_dump(frontmatter, sort_keys=True, allow_unicode=True).strip()
    normalized_body = body.lstrip("\n")
    if normalized_body:
        return f"---\n{dumped}\n---\n\n{normalized_body.rstrip()}\n"
    return f"---\n{dumped}\n---\n"


def _write_text(path: str, content: str) -> dict[str, Any]:
    _ensure_parent_folder(path)
    _rclone(["rcat", _join_remote(path)], stdin_text=content)
    state = _read_note_state(path)
    result = {
        "path": path,
        "sha256": state.sha256,
        "size_bytes": state.size_bytes,
        "modified_at": state.modified_at,
    }
    requested_at = _request_sync()
    if requested_at:
        result["sync_requested_at"] = requested_at
    return result


def _archive_destination(path: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{ARCHIVE_ROOT}/{stamp}/{path}"


def _request_sync() -> str | None:
    if not SYNC_REQUEST_FILE:
        return None
    directory = os.path.dirname(SYNC_REQUEST_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    stamp = datetime.now(UTC).isoformat()
    with open(SYNC_REQUEST_FILE, "w", encoding="utf-8") as handle:
        handle.write(stamp)
    logger.info("Requested mirror sync via %s", SYNC_REQUEST_FILE)
    return stamp


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


@mcp.custom_route("/info", methods=["GET"])
async def info(_: Request) -> JSONResponse:
    return JSONResponse(
        {
            "service": "vault-writer-mcp",
            "remote": REMOTE_NAME,
            "remote_root": REMOTE_ROOT,
            "archive_root": ARCHIVE_ROOT,
            "allow_hard_delete": ALLOW_HARD_DELETE,
            "no_check_certificate": NO_CHECK_CERT,
        }
    )


@mcp.tool
def stat_path(path: str) -> dict[str, Any]:
    """Return metadata for a note or folder path relative to the vault root."""
    normalized = _normalize_folder_path(path)
    metadata = _lsjson_stat(normalized)
    if metadata is None:
        raise WriterError(f"path not found: {normalized or '.'}")
    metadata["path"] = normalized
    return metadata


@mcp.tool
def list_folder(path: str = "", recursive: bool = False) -> list[dict[str, Any]]:
    """List notes and folders below a relative vault path."""
    normalized = _normalize_folder_path(path)
    args = ["lsjson"]
    if recursive:
        args.append("--recursive")
    args.append(_join_remote(normalized))
    payload = json.loads(_rclone(args))
    results: list[dict[str, Any]] = []
    for item in payload:
        item_path = item.get("Path") or item.get("Name") or ""
        if normalized and item_path and not item_path.startswith(normalized):
            item_path = f"{normalized}/{item_path}".strip("/")
        item["path"] = item_path
        results.append(item)
    return results


@mcp.tool
def read_note(path: str) -> dict[str, Any]:
    """Read a Markdown note and return its content plus a sha256 concurrency token."""
    normalized = _normalize_note_path(path)
    state = _read_note_state(normalized)
    return {
        "path": normalized,
        "content": state.content,
        "sha256": state.sha256,
        "size_bytes": state.size_bytes,
        "modified_at": state.modified_at,
    }


@mcp.tool
def write_note(path: str, content: str, expected_sha256: str | None = None) -> dict[str, Any]:
    """Create or overwrite a note. Use expected_sha256 to avoid overwriting concurrent edits."""
    normalized = _normalize_note_path(path)
    _assert_expected(normalized, expected_sha256)
    return _write_text(normalized, content)


@mcp.tool
def upsert_frontmatter(
    path: str,
    fields: dict[str, Any],
    expected_sha256: str | None = None,
    create_if_missing: bool = False,
    body_if_missing: str = "",
) -> dict[str, Any]:
    """Merge YAML frontmatter fields into a note, creating the note only when allowed."""
    normalized = _normalize_note_path(path)
    current = _assert_expected(normalized, expected_sha256)
    if current is None:
        if not create_if_missing:
            raise WriterError(f"note not found: {normalized}")
        frontmatter = dict(fields)
        content = _render_note(frontmatter, body_if_missing)
        result = _write_text(normalized, content)
        result["created"] = True
        return result
    frontmatter, body = _split_frontmatter(current.content)
    frontmatter.update(fields)
    result = _write_text(normalized, _render_note(frontmatter, body))
    result["created"] = False
    return result


@mcp.tool
def append_links(
    path: str,
    links: list[str],
    heading: str = "Related",
    expected_sha256: str | None = None,
) -> dict[str, Any]:
    """Append missing wikilinks under a heading without duplicating links already present."""
    normalized = _normalize_note_path(path)
    current = _assert_expected(normalized, expected_sha256)
    if current is None:
        raise WriterError(f"note not found: {normalized}")
    cleaned_links = []
    for link in links:
        target = link.strip()
        if not target:
            continue
        if target.startswith("[[") and target.endswith("]]"):
            cleaned_links.append(target)
        else:
            cleaned_links.append(f"[[{target}]]")
    if not cleaned_links:
        raise WriterError("at least one non-empty link is required")
    body = current.content.rstrip()
    missing = [link for link in cleaned_links if link not in body]
    if not missing:
        return {
            "path": normalized,
            "sha256": current.sha256,
            "size_bytes": current.size_bytes,
            "modified_at": current.modified_at,
            "appended": [],
        }
    heading_line = f"## {heading.strip()}"
    if heading_line not in body:
        body = f"{body}\n\n{heading_line}\n"
    if not body.endswith("\n"):
        body += "\n"
    body += "\n".join(f"- {link}" for link in missing) + "\n"
    result = _write_text(normalized, body)
    result["appended"] = missing
    return result


@mcp.tool
def move_note(from_path: str, to_path: str, expected_sha256: str | None = None) -> dict[str, Any]:
    """Move or rename a note inside the vault."""
    source = _normalize_note_path(from_path)
    target = _normalize_note_path(to_path)
    _assert_expected(source, expected_sha256)
    _ensure_parent_folder(target)
    _rclone(["moveto", _join_remote(source), _join_remote(target)])
    state = _read_note_state(target)
    result = {
        "from_path": source,
        "to_path": target,
        "sha256": state.sha256,
        "size_bytes": state.size_bytes,
        "modified_at": state.modified_at,
    }
    requested_at = _request_sync()
    if requested_at:
        result["sync_requested_at"] = requested_at
    return result


@mcp.tool
def archive_note(path: str, expected_sha256: str | None = None) -> dict[str, Any]:
    """Archive a note by moving it under the curator archive root with a timestamped prefix."""
    normalized = _normalize_note_path(path)
    _assert_expected(normalized, expected_sha256)
    archive_path = _normalize_note_path(_archive_destination(normalized))
    _ensure_parent_folder(archive_path)
    _rclone(["moveto", _join_remote(normalized), _join_remote(archive_path)])
    state = _read_note_state(archive_path)
    result = {
        "from_path": normalized,
        "archive_path": archive_path,
        "sha256": state.sha256,
        "size_bytes": state.size_bytes,
        "modified_at": state.modified_at,
    }
    requested_at = _request_sync()
    if requested_at:
        result["sync_requested_at"] = requested_at
    return result


@mcp.tool
def delete_note(path: str, expected_sha256: str | None = None, hard_delete: bool = False) -> dict[str, Any]:
    """Delete a note. By default it archives instead of hard-deleting; hard delete is opt-in and env-gated."""
    normalized = _normalize_note_path(path)
    _assert_expected(normalized, expected_sha256)
    if not hard_delete:
        archived = archive_note(normalized, expected_sha256=expected_sha256)
        archived["mode"] = "archived"
        return archived
    if not ALLOW_HARD_DELETE:
        raise WriterError("hard_delete is disabled by CURATOR_ALLOW_HARD_DELETE=false")
    _rclone(["deletefile", _join_remote(normalized)])
    result = {"path": normalized, "mode": "hard_deleted"}
    requested_at = _request_sync()
    if requested_at:
        result["sync_requested_at"] = requested_at
    return result


if __name__ == "__main__":
    mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
