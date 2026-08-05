# Changelog

Update `## Unreleased` on meaningful changes:
- `Added`: new user-visible features or services
- `Fixed`: behavior corrections, reliability fixes, or regressions
- `Docs`: README, profile, or operational guidance changes
- `Chore`: repo maintenance, tooling, or internal ops work with no behavior change

## Unreleased

### Added
- `curator-context-mcp` read-only MCP service on `8021` that turns one curator question into a deterministic curated context object from a single reader search.
- `vault-ingest` service for extracting non-Markdown vault documents into mirror-local Markdown shadow notes for indexing.
- Small observability stack with Grafana, Loki, Prometheus, cAdvisor, node-exporter, and Alloy for logs and basic host/container metrics.
- Asset lifecycle tools in `vault-writer-mcp` for moving, archiving, and deleting files stored under `NoteName_assets/`.

### Fixed
- `curator-context-mcp` response parsing for real `markdown-vault-mcp` payloads, including double-serialized results, SSE handling, empty notification responses, hit extraction, score normalization, bucket filtering, and abstention on weak or non-curated matches.
- `vault-ingest` PDF extraction support.
- `vault-writer-mcp` note moves, archives, and deletes now carry sibling `NoteName_assets/` folders and record per-file sync hints so attachment changes reach the mirror quickly.

### Docs
- Clarified curator workflow expectations around reader reindexing and post-mutation verification.

### Chore
- Added changelog tracking so feature, fix, doc, and ops changes have a single high-level history file.
