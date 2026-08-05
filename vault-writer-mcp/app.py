from __future__ import annotations

import hashlib
import json
import logging
import os
import posixpath
import re
import subprocess
import sys
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
CHANGED_PATHS_FILE = os.getenv("CHANGED_PATHS_FILE", "/control/changed-paths.log")
ASSET_FOLDER_SUFFIX = "_assets"

mcp = FastMCP(
    "vault-writer-mcp",
    instructions=(
        "Writes Markdown notes directly to the source WebDAV vault through rclone. "
        "Prefer archive_note over delete_note. Use expected_sha256 on destructive note edits "
        "to avoid clobbering concurrent changes."
    ),
    version="0.1.1",
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


def _normalize_vault_path(path: str) -> str:
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
        raise WriterError("path must target a file or folder")

    return normalized


def _normalize_note_path(path: str) -> str:
    normalized = _normalize_vault_path(path)
    if not normalized.endswith(".md"):
        raise WriterError("only .md notes are supported by this writer")
    return normalized


def _asset_owner_folder(path: str) -> str | None:
    owners = [part for part in PurePosixPath(path).parts if part.endswith(ASSET_FOLDER_SUFFIX)]
    if not owners:
        return None
    if len(owners) > 1:
        raise WriterError("asset paths cannot nest multiple *_assets folders")
    owner = owners[0]
    if owner == ASSET_FOLDER_SUFFIX:
        raise WriterError("asset folder names must keep the NoteName_assets convention")
    return owner


def _normalize_asset_path(path: str) -> str:
    normalized = _normalize_vault_path(path)
    if _asset_owner_folder(normalized) is None:
        raise WriterError("asset paths must live under a NoteName_assets folder")
    return normalized


def _require_same_asset_owner(source: str, target: str) -> None:
    if _asset_owner_folder(source) != _asset_owner_folder(target):
        raise WriterError("asset moves must stay within the same NoteName_assets owner")


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


def _lsjson(path: str, *, recursive: bool = False) -> list[dict[str, Any]]:
    args = ["lsjson"]
    if recursive:
        args.append("--recursive")
    args.append(_join_remote(path))
    payload = json.loads(_rclone(args))
    if not isinstance(payload, list):
        raise WriterError("lsjson returned an unexpected payload")
    return payload


def _path_metadata(path: str) -> dict[str, Any]:
    metadata = _lsjson_stat(path)
    if metadata is None:
        raise WriterError(f"path not found: {path}")
    return metadata


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


def _asset_folder_for_note(path: str) -> str:
    note = PurePosixPath(path)
    folder_name = f"{note.stem}{ASSET_FOLDER_SUFFIX}"
    parent = note.parent.as_posix()
    if parent in ("", "."):
        return folder_name
    return f"{parent}/{folder_name}"


def _note_folder(path: str) -> str:
    parent = PurePosixPath(path).parent.as_posix()
    return "" if parent in ("", ".") else parent


def _list_file_paths(path: str, metadata: dict[str, Any] | None = None) -> list[str]:
    metadata = metadata or _path_metadata(path)
    if not metadata.get("IsDir"):
        return [path]
    files: list[str] = []
    for item in _lsjson(path, recursive=True):
        if item.get("IsDir"):
            continue
        item_path = item.get("Path") or item.get("Name") or ""
        if item_path:
            files.append(posixpath.normpath(f"{path}/{item_path}"))
    return files


def _resolve_note_reference(note_path: str, reference: str) -> str:
    clean = reference.strip()
    if not clean:
        raise WriterError("empty asset reference")
    if clean.startswith("/"):
        return _normalize_vault_path(clean.lstrip("/"))
    note_folder = _note_folder(note_path)
    joined = posixpath.join(note_folder, clean) if note_folder else clean
    return _normalize_vault_path(joined)


def _relative_from_note(note_path: str, target_path: str) -> str:
    note_folder = _note_folder(note_path)
    if not note_folder:
        return target_path
    return posixpath.relpath(target_path, note_folder)


def _is_external_reference(reference: str) -> bool:
    lowered = reference.strip().lower()
    return lowered.startswith(("http://", "https://", "data:", "mailto:", "ftp://"))


def _split_wikilink_target(target: str) -> tuple[str, str]:
    pipe_index = target.find("|")
    suffix = ""
    if pipe_index != -1:
        suffix = target[pipe_index:]
        target = target[:pipe_index]
    hash_index = target.find("#")
    if hash_index != -1:
        suffix = target[hash_index:] + suffix
        target = target[:hash_index]
    return target, suffix


def _split_markdown_target(target: str) -> tuple[str, str]:
    stripped = target.strip()
    if stripped.startswith("<"):
        closing = stripped.find(">")
        if closing != -1:
            return stripped[1:closing], stripped[closing + 1 :]
    match = re.match(r"([^\s)]+)(.*)", stripped)
    if not match:
        return stripped, ""
    return match.group(1), match.group(2)


def _rewrite_markdown_target(path_part: str, suffix: str) -> str:
    if suffix:
        return f"{path_part}{suffix}"
    return path_part


def _organize_referenced_assets(note_path: str, content: str) -> tuple[str, list[dict[str, str]]]:
    asset_folder = _asset_folder_for_note(note_path)
    planned_moves: dict[str, str] = {}
    moved_assets: list[dict[str, str]] = []

    def plan_asset(reference: str) -> str:
        if not reference or reference.startswith("#") or _is_external_reference(reference):
            return reference
        resolved = _resolve_note_reference(note_path, reference)
        metadata = _lsjson_stat(resolved)
        if metadata is None or metadata.get("IsDir"):
            return reference
        if resolved.endswith(".md"):
            return reference
        if _asset_owner_folder(resolved) == PurePosixPath(asset_folder).name:
            return _relative_from_note(note_path, resolved)
        target = posixpath.join(asset_folder, PurePosixPath(resolved).name)
        previous = planned_moves.get(resolved)
        if previous is None:
            for source_path, target_path in planned_moves.items():
                if target_path == target and source_path != resolved:
                    raise WriterError(f"asset name collision while organizing note assets: {target}")
            planned_moves[resolved] = target
            moved_assets.append({"from_path": resolved, "to_path": target})
        return _relative_from_note(note_path, planned_moves[resolved])

    wikilink_re = re.compile(r"(!?\[\[)([^\]]+)(\]\])")
    markdown_re = re.compile(r"(!?\[[^\]]*\]\()([^\)]+)(\))")

    def replace_wikilink(match: re.Match[str]) -> str:
        target, suffix = _split_wikilink_target(match.group(2))
        rewritten = plan_asset(target)
        if rewritten == target:
            return match.group(0)
        return f"{match.group(1)}{rewritten}{suffix}{match.group(3)}"

    def replace_markdown(match: re.Match[str]) -> str:
        target, suffix = _split_markdown_target(match.group(2))
        rewritten = plan_asset(target)
        if rewritten == target:
            return match.group(0)
        return f"{match.group(1)}{_rewrite_markdown_target(rewritten, suffix)}{match.group(3)}"

    updated = wikilink_re.sub(replace_wikilink, content)
    updated = markdown_re.sub(replace_markdown, updated)
    return updated, moved_assets


def _record_move_for_sync(source: str, target: str, metadata: dict[str, Any] | None = None) -> None:
    metadata = metadata or _path_metadata(source)
    source_files = _list_file_paths(source, metadata)
    if metadata.get("IsDir"):
        for source_file in source_files:
            relative = posixpath.relpath(source_file, source)
            target_file = posixpath.normpath(f"{target}/{relative}")
            _record_changed_path("write", target_file)
            _record_changed_path("delete", source_file)
        return
    _record_changed_path("write", target)
    _record_changed_path("delete", source)


def _record_delete_for_sync(path: str, metadata: dict[str, Any] | None = None) -> None:
    metadata = metadata or _path_metadata(path)
    for file_path in _list_file_paths(path, metadata):
        _record_changed_path("delete", file_path)


def _delete_path(path: str, metadata: dict[str, Any] | None = None) -> None:
    metadata = metadata or _path_metadata(path)
    command = "purge" if metadata.get("IsDir") else "deletefile"
    _rclone([command, _join_remote(path)])


def _move_path(source: str, target: str) -> None:
    _ensure_parent_folder(target)
    _rclone(["moveto", _join_remote(source), _join_remote(target)])


def _result_from_metadata(path: str, metadata: dict[str, Any]) -> dict[str, Any]:
    result = {
        "path": path,
        "size_bytes": metadata.get("Size"),
        "modified_at": metadata.get("ModTime"),
    }
    if metadata.get("IsDir"):
        result["is_dir"] = True
    return result


def _request_sync_result() -> dict[str, Any]:
    result: dict[str, Any] = {}
    requested_at = _request_sync()
    if requested_at:
        result["sync_requested_at"] = requested_at
    return result


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
    _record_changed_path("write", path)
    result.update(_request_sync_result())
    return result


def _write_text_without_sync(path: str, content: str) -> dict[str, Any]:
    _ensure_parent_folder(path)
    _rclone(["rcat", _join_remote(path)], stdin_text=content)
    state = _read_note_state(path)
    result = {
        "path": path,
        "sha256": state.sha256,
        "size_bytes": state.size_bytes,
        "modified_at": state.modified_at,
    }
    _record_changed_path("write", path)
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


def _record_changed_path(operation: str, path: str) -> None:
    if not CHANGED_PATHS_FILE:
        return
    directory = os.path.dirname(CHANGED_PATHS_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(CHANGED_PATHS_FILE, "a", encoding="utf-8") as handle:
        handle.write(f"{operation}|{path}\n")
    logger.info("Recorded changed path: %s %s", operation, path)


def _note_result(path: str) -> dict[str, Any]:
    state = _read_note_state(path)
    return {
        "path": path,
        "sha256": state.sha256,
        "size_bytes": state.size_bytes,
        "modified_at": state.modified_at,
    }


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
def request_sync() -> dict[str, Any]:
    """Drop a sync request into the shared sync-control volume so vault-sync
    refreshes the local mirror on its next loop iteration. Use this when a
    human edits the vault directly through NAS WebDAV and the curator wants
    the mirror refreshed without performing a writer mutation first."""
    requested_at = _request_sync()
    if not requested_at:
        raise WriterError("sync request disabled (SYNC_REQUEST_FILE not configured)")
    return {"sync_requested_at": requested_at}


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
    payload = _lsjson(normalized, recursive=recursive)
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
def organize_note_assets(path: str, expected_sha256: str | None = None) -> dict[str, Any]:
    """Move referenced local assets into NoteName_assets/ and rewrite the note links."""
    normalized = _normalize_note_path(path)
    current = _assert_expected(normalized, expected_sha256)
    if current is None:
        raise WriterError(f"note not found: {normalized}")

    updated_content, planned_assets = _organize_referenced_assets(normalized, current.content)
    if not planned_assets:
        return {
            "path": normalized,
            "sha256": current.sha256,
            "size_bytes": current.size_bytes,
            "modified_at": current.modified_at,
            "assets_moved": [],
            "rewritten": False,
        }

    for asset in planned_assets:
        source = asset["from_path"]
        target = asset["to_path"]
        metadata = _path_metadata(source)
        _record_move_for_sync(source, target, metadata)
        _move_path(source, target)

    result = _write_text_without_sync(normalized, updated_content)
    result["assets_moved"] = planned_assets
    result["rewritten"] = updated_content != current.content
    result.update(_request_sync_result())
    return result


@mcp.tool
def move_note(from_path: str, to_path: str, expected_sha256: str | None = None) -> dict[str, Any]:
    """Move or rename a note inside the vault."""
    source = _normalize_note_path(from_path)
    target = _normalize_note_path(to_path)
    _assert_expected(source, expected_sha256)
    source_assets = _asset_folder_for_note(source)
    target_assets = _asset_folder_for_note(target)
    asset_metadata = _lsjson_stat(source_assets)
    _move_path(source, target)
    if asset_metadata is not None:
        _record_move_for_sync(source_assets, target_assets, asset_metadata)
        _move_path(source_assets, target_assets)
    result = {
        "from_path": source,
        "to_path": target,
    }
    result.update(_note_result(target))
    result["assets_moved"] = asset_metadata is not None
    if asset_metadata is not None:
        result["assets_to_path"] = target_assets
    _record_changed_path("write", target)
    _record_changed_path("delete", source)
    result.update(_request_sync_result())
    return result


@mcp.tool
def move_asset(from_path: str, to_path: str) -> dict[str, Any]:
    """Move or rename an asset inside the same NoteName_assets owner."""
    source = _normalize_asset_path(from_path)
    target = _normalize_asset_path(to_path)
    _require_same_asset_owner(source, target)
    metadata = _path_metadata(source)
    _record_move_for_sync(source, target, metadata)
    _move_path(source, target)
    result = {
        "from_path": source,
        "to_path": target,
    }
    result.update(_result_from_metadata(target, _path_metadata(target)))
    result.update(_request_sync_result())
    return result


@mcp.tool
def archive_note(path: str, expected_sha256: str | None = None) -> dict[str, Any]:
    """Archive a note by moving it under the curator archive root with a timestamped prefix."""
    normalized = _normalize_note_path(path)
    _assert_expected(normalized, expected_sha256)
    archive_path = _normalize_note_path(_archive_destination(normalized))
    source_assets = _asset_folder_for_note(normalized)
    archive_assets = _asset_folder_for_note(archive_path)
    asset_metadata = _lsjson_stat(source_assets)
    _move_path(normalized, archive_path)
    if asset_metadata is not None:
        _record_move_for_sync(source_assets, archive_assets, asset_metadata)
        _move_path(source_assets, archive_assets)
    result = {
        "from_path": normalized,
        "archive_path": archive_path,
    }
    result.update(_note_result(archive_path))
    result["assets_archived"] = asset_metadata is not None
    if asset_metadata is not None:
        result["assets_archive_path"] = archive_assets
    _record_changed_path("write", archive_path)
    _record_changed_path("delete", normalized)
    result.update(_request_sync_result())
    return result


@mcp.tool
def archive_asset(path: str) -> dict[str, Any]:
    """Archive an asset under the curator archive root."""
    normalized = _normalize_asset_path(path)
    archive_path = _normalize_asset_path(_archive_destination(normalized))
    _require_same_asset_owner(normalized, archive_path)
    metadata = _path_metadata(normalized)
    _record_move_for_sync(normalized, archive_path, metadata)
    _move_path(normalized, archive_path)
    result = {
        "from_path": normalized,
        "archive_path": archive_path,
    }
    result.update(_result_from_metadata(archive_path, _path_metadata(archive_path)))
    result.update(_request_sync_result())
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
    source_assets = _asset_folder_for_note(normalized)
    asset_metadata = _lsjson_stat(source_assets)
    if asset_metadata is not None:
        _record_delete_for_sync(source_assets, asset_metadata)
        _delete_path(source_assets, asset_metadata)
    _delete_path(normalized, {"IsDir": False})
    result = {"path": normalized, "mode": "hard_deleted"}
    result["assets_deleted"] = asset_metadata is not None
    _record_changed_path("delete", normalized)
    result.update(_request_sync_result())
    return result


@mcp.tool
def delete_asset(path: str, hard_delete: bool = False) -> dict[str, Any]:
    """Delete an asset. By default it archives instead of hard-deleting; hard delete is opt-in and env-gated."""
    normalized = _normalize_asset_path(path)
    if not hard_delete:
        archived = archive_asset(normalized)
        archived["mode"] = "archived"
        return archived
    if not ALLOW_HARD_DELETE:
        raise WriterError("hard_delete is disabled by CURATOR_ALLOW_HARD_DELETE=false")
    metadata = _path_metadata(normalized)
    _record_delete_for_sync(normalized, metadata)
    _delete_path(normalized, metadata)
    result = {"path": normalized, "mode": "hard_deleted"}
    result.update(_request_sync_result())
    return result


def selfcheck() -> None:
    assert _normalize_note_path("Notes/Project.md") == "Notes/Project.md"
    assert _normalize_asset_path("Notes/Project_assets/image.png") == "Notes/Project_assets/image.png"
    assert _asset_folder_for_note("Notes/Project.md") == "Notes/Project_assets"

    try:
        _normalize_note_path("Notes/Project_assets/image.png")
    except WriterError:
        pass
    else:
        raise AssertionError("non-Markdown asset path should not pass note validation")

    try:
        _normalize_asset_path("Notes/image.png")
    except WriterError:
        pass
    else:
        raise AssertionError("asset path outside NoteName_assets should be rejected")

    _require_same_asset_owner("Notes/Project_assets/image.png", "Archive/Project_assets/image.png")

    try:
        _require_same_asset_owner("Notes/Project_assets/image.png", "Archive/Other_assets/image.png")
    except WriterError:
        pass
    else:
        raise AssertionError("asset move across owners should be rejected")

    original_lsjson_stat = globals()["_lsjson_stat"]

    def fake_lsjson_stat(path: str) -> dict[str, Any] | None:
        known = {
            "Notes/images/pic.png": {"IsDir": False, "Size": 1, "ModTime": "now"},
            "Notes/docs/file.pdf": {"IsDir": False, "Size": 1, "ModTime": "now"},
            "Notes/Other.md": {"IsDir": False, "Size": 1, "ModTime": "now"},
        }
        return known.get(path)

    globals()["_lsjson_stat"] = fake_lsjson_stat
    try:
        rewritten, moved = _organize_referenced_assets(
            "Notes/Project.md",
            "![img](images/pic.png)\n![[docs/file.pdf|PDF]]\n[[Other.md]]\n",
        )
    finally:
        globals()["_lsjson_stat"] = original_lsjson_stat

    assert rewritten == "![img](Project_assets/pic.png)\n![[Project_assets/file.pdf|PDF]]\n[[Other.md]]\n"
    assert sorted(moved, key=lambda item: item["from_path"]) == [
        {"from_path": "Notes/docs/file.pdf", "to_path": "Notes/Project_assets/file.pdf"},
        {"from_path": "Notes/images/pic.png", "to_path": "Notes/Project_assets/pic.png"},
    ]

    print("selfcheck ok")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selfcheck":
        selfcheck()
        raise SystemExit(0)
    mcp.run(transport="http", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
