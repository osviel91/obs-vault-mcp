from __future__ import annotations

import hashlib
import logging
import os
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("vault-ingest")

VAULT_ROOT = Path(os.getenv("VAULT_ROOT", "/vault")).resolve()
INGEST_ROOT = VAULT_ROOT / ".ingest"
INGEST_INTERVAL_SECONDS = int(os.getenv("INGEST_INTERVAL_SECONDS", "600"))
OCR_PDFS = os.getenv("OCR_PDFS", "true").lower() == "true"
OCR_LANGS = os.getenv("OCR_LANGS", "spa+eng")
SUPPORTED_EXTENSIONS = {
    ".csv",
    ".doc",
    ".docx",
    ".htm",
    ".html",
    ".json",
    ".pdf",
    ".pptx",
    ".txt",
    ".xlsx",
}
EXCLUDED_DIRS = {".git", ".ingest", ".markdown_vault_mcp", ".obsidian", ".trash"}
EXCLUDED_FILES = {".webdav-sync-ready"}
_RUNNING = True


def _stop(_: int, __: object) -> None:
    global _RUNNING
    _RUNNING = False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_relpath(path: Path) -> str:
    return path.resolve().relative_to(VAULT_ROOT).as_posix()


def _shadow_path_for(source_path: Path) -> Path:
    relative = source_path.resolve().relative_to(VAULT_ROOT)
    return INGEST_ROOT / relative.parent / f"{relative.name}.md"


def _parse_frontmatter(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}
    payload: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        payload[key.strip()] = value.strip().strip('"')
    return payload


def _quote_frontmatter(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_shadow_note(source_path: str, source_sha256: str, source_size: int, source_mtime: str, markdown: str, *, ocr_applied: bool) -> str:
    body = markdown.strip()
    frontmatter = [
        "---",
        f"source_path: {_quote_frontmatter(source_path)}",
        f"source_sha256: {_quote_frontmatter(source_sha256)}",
        f"source_size: {source_size}",
        f"source_mtime: {_quote_frontmatter(source_mtime)}",
        f"ingested_at: {_quote_frontmatter(datetime.now(UTC).isoformat())}",
        'ingest_kind: "shadow"',
        f"ocr_applied: {'true' if ocr_applied else 'false'}",
        "---",
        "",
    ]
    if body:
        frontmatter.append(body)
        frontmatter.append("")
    return "\n".join(frontmatter)


def _run_command(args: list[str]) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        stderr = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(stderr or f"command failed: {' '.join(args)}")
    return proc.stdout


def _extract_markdown_with_markitdown(path: Path) -> str:
    return _run_command(["markitdown", str(path)])


def _ocr_pdf(source_path: Path) -> tuple[Path, bool, tempfile.TemporaryDirectory[str] | None]:
    if source_path.suffix.lower() != ".pdf" or not OCR_PDFS:
        return source_path, False, None
    temp_dir = tempfile.TemporaryDirectory(prefix="vault-ingest-")
    output_path = Path(temp_dir.name) / source_path.name
    args = [
        "ocrmypdf",
        "--skip-text",
        "--quiet",
        "--language",
        OCR_LANGS,
        str(source_path),
        str(output_path),
    ]
    try:
        _run_command(args)
    except RuntimeError as exc:
        temp_dir.cleanup()
        logger.warning("OCR failed for %s: %s", source_path, exc)
        return source_path, False, None
    return output_path, True, temp_dir


def _source_mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).isoformat()


def _is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_EXTENSIONS


def _iter_source_documents() -> list[Path]:
    documents: list[Path] = []
    for root, dirs, files in os.walk(VAULT_ROOT):
        dirs[:] = [name for name in dirs if name not in EXCLUDED_DIRS]
        root_path = Path(root)
        for file_name in files:
            if file_name in EXCLUDED_FILES:
                continue
            path = root_path / file_name
            if not _is_supported(path):
                continue
            documents.append(path)
    documents.sort()
    return documents


def _write_shadow_note(shadow_path: Path, content: str) -> None:
    shadow_path.parent.mkdir(parents=True, exist_ok=True)
    shadow_path.write_text(content, encoding="utf-8")


def _ingest_one(source_path: Path) -> None:
    source_relpath = _source_relpath(source_path)
    source_sha256 = _sha256_file(source_path)
    shadow_path = _shadow_path_for(source_path)
    if shadow_path.exists():
        existing = _parse_frontmatter(shadow_path.read_text(encoding="utf-8"))
        if existing.get("source_sha256") == source_sha256:
            return

    ocr_path, ocr_applied, temp_dir = _ocr_pdf(source_path)
    try:
        markdown = _extract_markdown_with_markitdown(ocr_path)
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()
    rendered = _render_shadow_note(
        source_relpath,
        source_sha256,
        source_path.stat().st_size,
        _source_mtime(source_path),
        markdown,
        ocr_applied=ocr_applied,
    )
    _write_shadow_note(shadow_path, rendered)
    logger.info("Ingested %s -> %s", source_relpath, shadow_path.relative_to(VAULT_ROOT).as_posix())


def _cleanup_orphans() -> None:
    if not INGEST_ROOT.exists():
        return
    for shadow_path in sorted(INGEST_ROOT.rglob("*.md")):
        if not shadow_path.is_file():
            continue
        existing = _parse_frontmatter(shadow_path.read_text(encoding="utf-8"))
        source_relpath = existing.get("source_path")
        if not source_relpath:
            source_relpath = shadow_path.relative_to(INGEST_ROOT).as_posix()[:-3]
        source_path = VAULT_ROOT / source_relpath
        if source_path.exists():
            continue
        shadow_path.unlink()
        logger.info("Removed orphan shadow note %s", shadow_path.relative_to(VAULT_ROOT).as_posix())
    for directory in sorted(INGEST_ROOT.rglob("*"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()


def run_once() -> None:
    if not VAULT_ROOT.exists():
        raise RuntimeError(f"vault root does not exist: {VAULT_ROOT}")
    INGEST_ROOT.mkdir(parents=True, exist_ok=True)
    for source_path in _iter_source_documents():
        try:
            _ingest_one(source_path)
        except Exception as exc:  # ponytail: log and continue, one bad doc should not block the whole pass
            logger.warning("Failed to ingest %s: %s", source_path, exc)
    _cleanup_orphans()


def _selfcheck() -> None:
    fake_source = VAULT_ROOT / "Docs/example.pdf"
    expected = INGEST_ROOT / "Docs/example.pdf.md"
    assert _shadow_path_for(fake_source) == expected
    rendered = _render_shadow_note("Docs/example.pdf", "abc", 12, "2026-01-01T00:00:00+00:00", "Body", ocr_applied=True)
    parsed = _parse_frontmatter(rendered)
    assert parsed["source_path"] == "Docs/example.pdf"
    assert parsed["source_sha256"] == "abc"
    assert parsed["ocr_applied"] == "true"


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "selfcheck":
        _selfcheck()
        return
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    logger.info(
        "Starting vault ingest loop: vault_root=%s ingest_root=%s interval=%ss ocr_pdfs=%s ocr_langs=%s",
        VAULT_ROOT,
        INGEST_ROOT,
        INGEST_INTERVAL_SECONDS,
        OCR_PDFS,
        OCR_LANGS,
    )
    while _RUNNING:
        started_at = time.time()
        run_once()
        elapsed = time.time() - started_at
        sleep_for = max(1, INGEST_INTERVAL_SECONDS - int(elapsed))
        if not _RUNNING:
            break
        time.sleep(sleep_for)


if __name__ == "__main__":
    main()
