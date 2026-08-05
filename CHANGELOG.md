# Changelog

## Unreleased

### Added
- `curator-context-mcp` read-only MCP service on `8021` that turns one curator question into a deterministic curated context object from a single reader search.
- `vault-ingest` service for extracting non-Markdown vault documents into mirror-local Markdown shadow notes for indexing.
- Small observability stack with Grafana, Loki, Prometheus, cAdvisor, node-exporter, and Alloy for logs and basic host/container metrics.

### Fixed
- `curator-context-mcp` response parsing for real `markdown-vault-mcp` payloads, including double-serialized results, SSE handling, empty notification responses, hit extraction, score normalization, bucket filtering, and abstention on weak or non-curated matches.
- `vault-ingest` PDF extraction support.

### Docs
- Clarified curator workflow expectations around reader reindexing and post-mutation verification.

### Chore
- Added changelog tracking so feature, fix, doc, and ops changes have a single high-level history file.
