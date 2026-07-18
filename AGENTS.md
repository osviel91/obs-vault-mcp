# AGENTS.md

## Scope
- This repo is a Docker Compose deployment for mirroring an Obsidian vault from WebDAV, exposing it through `markdown-vault-mcp`, providing a separate WebDAV-backed writer MCP for curator agents, and a thin context MCP that turns one question into a curated context object.

## High-Value Files
- `compose.yaml`: source of truth for service wiring, env vars, volumes, healthcheck, port mapping, and startup order.
- `.env.example`: source of truth for required stack variables and expected defaults.
- `README.md`: operational workflow for Portainer deploys and runtime verification.
- `vault-writer-mcp/app.py`: source of truth for curator write safety rules and available write tools.
- `curator-context-mcp/app.py`: source of truth for the `consultar_contexto` RAG-lite context tool (classification buckets, boost rules, drift behaviour).
- `hermes/knowledge-curator.md`: source of truth for the Hermes curator profile prompt and the intended two-MCP operating model.

## Deployment Model
- This stack is intended for Portainer Git stack deploys, not a local app dev loop.
- `vault-init` must complete successfully before both `vault-sync` and `markdown-vault-mcp` start (`depends_on.condition: service_completed_successfully`). Preserve that ordering unless the deployment model changes.

## Behavior That Is Easy To Break
- Sync is intentionally one-way: `rclone sync` mirrors `NAS WebDAV -> /vault`. Do not introduce workflows that treat the Docker mirror as writable state.
- `vault-sync` logs failures and keeps the previous mirror instead of deleting local data after a failed sync. Keep that failure behavior intact unless explicitly changing recovery semantics.
- `markdown-vault-mcp` is deliberately read-only via `MARKDOWN_VAULT_MCP_READ_ONLY=true`.
- Curator-style writes must go through `vault-writer-mcp`, which talks directly to WebDAV through `rclone`, not through the local mirror volume.
- Writer mutations also drop a sync request into the shared `sync-control` volume so `vault-sync` can refresh the mirror quickly; the reader is still eventually consistent with the writer.
- On a writer-triggered request, `vault-sync` first applies the per-path records in `changed-paths.log` with one `rclone copyto` (or `deletefile`) per path, then runs a single `rclone sync` for unannounced changes. The per-path `copyto` bypasses the WebDAV directory listing cache: a direct GET on a specific file path returns bytes immediately even while the directory's `PROPFIND` listing is stale. The NAS WebDAV here takes several minutes to refresh directory listings after a write, so the per-path step is what keeps writer-to-mirror latency low. Scheduled syncs run a single pass. Do not remove the per-path `copyto` step without understanding this read-after-write race.
- The writer also exposes a public `request_sync` tool that drops a sync request without performing a writer mutation. Use it when a human edits the vault through NAS WebDAV directly and a curator wants the mirror refreshed on demand.
- Forcing a reindex of the read-only reader (`markdown-vault-mcp`) is the responsibility of the reader, not the writer. Agents should call the reader's own `reindex` / `build_embeddings` / `get_index_status` tools; the writer does not bridge to the reader.
- `MARKDOWN_VAULT_MCP_FILE_WATCHER=false` is set on purpose: the local mirror is populated by `vault-sync` from another container, and inotify does not see cross-container writes. Do not re-enable the watcher without first proving external edits land in the same container as the reader.
- The rclone remote is env-defined and name-sensitive: `compose.yaml` uses remote name `naswebdav`, so the env vars must stay `RCLONE_CONFIG_NASWEBDAV_*`.
- Exclusions matter in two places:
  - `rclone sync` excludes `/.markdown_vault_mcp/**`, `/.git/**`, and `/.trash/**`
  - MCP excludes `.obsidian/**,.trash/**,.git/**,.webdav-sync-ready`
- `WEBDAV_URL` may point either at the WebDAV base or directly at the vault root. If it points at the vault root, `WEBDAV_REMOTE_PATH` should be empty.
- `WEBDAV_NO_CHECK_CERTIFICATE=true` exists for NAS setups with self-signed certs or IP/hostname certificate mismatches. Do not remove it unless the deploy model changes.
- Writer safety defaults matter: `CURATOR_ALLOW_HARD_DELETE=false` and `CURATOR_ARCHIVE_ROOT=.curator-archive` are there to make curator deletes archival by default.
- `curator-context-mcp` is intentionally read-only and stateless: it issues exactly one `search` (hybrid) call to `markdown-vault-mcp` over MCP-HTTP per `consultar_contexto` invocation and post-processes the hits. Do not add caching, persistent state, direct volume mounts, or LLM-backed summaries to it — the value is that the tool is a deterministic, no-invention post-processor of the reader's index.
- `consultar_contexto` classifies hits by path prefix (`Curator/heuristics`, `Curator/decisions`, `Curator/contradictions`, `MOCs` / `MOC` segments, `.curator-archive/`) and applies a 2x score boost to heuristics and MOCs only. Persisted frontmatter markers (`status: obsolete|deprecated`, `confidence: low` matched in the snippet) relegate a hit to the `obsoletas_o_baja_confianza` bucket regardless of its path. Do not loosen the inbox discard rule (`Curator/inbox/**` is never returned) without an explicit decision; inbox is un-curated material.
- `consultar_contexto` returns an honest `summary` when the index is not queryable or no hit clears `umbral_similitud`, and never fabricates content. Preserve that contract; if the reader's hybrid `score` is not in [0,1] (RRF scores), the tool normalizes by the pool max — keep that normalization or replace it with a documented alternative.

## Runtime Facts
- Host endpoint: `http://HOST:8019/mcp`
- Writer endpoint: `http://HOST:8020/mcp`
- Context endpoint: `http://HOST:8021/mcp`
- Humans edit the vault directly through NAS WebDAV; Hermes should use the MCP endpoints, not WebDAV directly.
- Container listens on `8000`; Compose publishes `8019:8000`, `8020:8000`, `8021:8000`.
- Persistent volumes:
  - `obsidian-knowledge-vault`: disposable local mirror of WebDAV
  - `obsidian-knowledge-mcp-state`: persistent index, embeddings, and cache
  - `obsidian-knowledge-sync-control`: shared control volume used to trigger faster mirror refreshes after writer mutations
- `curator-context-mcp` mounts no volumes: all vault access goes through `markdown-vault-mcp` over MCP-HTTP (`READER_MCP_URL`).

## Useful Commands
- Validate Compose after edits: `docker compose --env-file .env.example config`
- Validate writer syntax after edits: `python3 -m py_compile vault-writer-mcp/app.py`
- Validate context service syntax after edits: `python3 -m py_compile curator-context-mcp/app.py`
- Run the context service self-check (no container needed): `python3 curator-context-mcp/app.py selfcheck`
- Check initial sync: `docker logs obsidian-vault-init`
- Follow periodic sync: `docker logs -f obsidian-vault-sync`
- Inspect mirrored files without the rclone entrypoint: `docker run --rm -v obsidian-knowledge-vault:/vault alpine find /vault -maxdepth 2 -type f`
- Follow MCP logs: `docker logs -f markdown-vault-mcp`
- Follow writer logs: `docker logs -f vault-writer-mcp`
- Follow context service logs: `docker logs -f curator-context-mcp`
- Trigger an immediate sync loop iteration: `docker restart obsidian-vault-sync`
- Force an on-demand mirror refresh from the writer MCP: call `request_sync` on `http://HOST:8020/mcp`
- Force an immediate reindex from the reader MCP: call `reindex` (or `build_embeddings` for vectors only) on `http://HOST:8019/mcp`; check progress with `get_index_status`
- Get a curated context object for a question: call `consultar_contexto` on `http://HOST:8021/mcp`

## Editing Guidance
- If you add or rename environment variables in `compose.yaml`, update `.env.example` and the variable table in `README.md` in the same change.
- Keep security assumptions aligned with the current docs: no real `.env` in git, private repo, and do not expose port `8019` publicly without adding protection.
