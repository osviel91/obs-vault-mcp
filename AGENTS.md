# AGENTS.md

## Scope
- This repo is a Docker Compose deployment for mirroring an Obsidian vault from WebDAV, exposing it through `markdown-vault-mcp`, and providing a separate WebDAV-backed writer MCP for curator agents.
- Most changes still land in `compose.yaml`, `.env.example`, `README.md`, and the small `vault-writer-mcp/` service.

## High-Value Files
- `compose.yaml`: source of truth for service wiring, env vars, volumes, healthcheck, port mapping, and startup order.
- `.env.example`: source of truth for required stack variables and expected defaults.
- `README.md`: operational workflow for Portainer deploys and runtime verification.
- `vault-writer-mcp/app.py`: source of truth for curator write safety rules and available write tools.
- `hermes/knowledge-curator.md`: source of truth for the Hermes curator profile prompt and the intended two-MCP operating model.

## Deployment Model
- This stack is intended for Portainer Git stack deploys, not a local app dev loop.
- `vault-init` must complete successfully before both `vault-sync` and `markdown-vault-mcp` start (`depends_on.condition: service_completed_successfully`). Preserve that ordering unless the deployment model changes.

## Behavior That Is Easy To Break
- Sync is intentionally one-way: `rclone sync` mirrors `NAS WebDAV -> /vault`. Do not introduce workflows that treat the Docker mirror as writable state.
- `vault-sync` logs failures and keeps the previous mirror instead of deleting local data after a failed sync. Keep that failure behavior intact unless explicitly changing recovery semantics.
- `markdown-vault-mcp` is deliberately read-only via `MARKDOWN_VAULT_MCP_READ_ONLY=true`.
- Curator-style writes must go through `vault-writer-mcp`, which talks directly to WebDAV through `rclone`, not through the local mirror volume.
- The rclone remote is env-defined and name-sensitive: `compose.yaml` uses remote name `naswebdav`, so the env vars must stay `RCLONE_CONFIG_NASWEBDAV_*`.
- Exclusions matter in two places:
  - `rclone sync` excludes `/.markdown_vault_mcp/**`, `/.git/**`, and `/.trash/**`
  - MCP excludes `.obsidian/**,.trash/**,.git/**,.webdav-sync-ready`
- `WEBDAV_URL` may point either at the WebDAV base or directly at the vault root. If it points at the vault root, `WEBDAV_REMOTE_PATH` should be empty.
- `WEBDAV_NO_CHECK_CERTIFICATE=true` exists for NAS setups with self-signed certs or IP/hostname certificate mismatches. Do not remove it unless the deploy model changes.
- Writer safety defaults matter: `CURATOR_ALLOW_HARD_DELETE=false` and `CURATOR_ARCHIVE_ROOT=.curator-archive` are there to make curator deletes archival by default.

## Runtime Facts
- Host endpoint: `http://HOST:8019/mcp`
- Writer endpoint: `http://HOST:8020/mcp`
- Humans edit the vault directly through NAS WebDAV; Hermes should use the MCP endpoints, not WebDAV directly.
- Container listens on `8000`; Compose publishes `8019:8000`.
- Persistent volumes:
  - `obsidian-knowledge-vault`: disposable local mirror of WebDAV
  - `obsidian-knowledge-mcp-state`: persistent index, embeddings, and cache

## Useful Commands
- Validate Compose after edits: `docker compose --env-file .env.example config`
- Validate writer syntax after edits: `python3 -m py_compile vault-writer-mcp/app.py`
- Check initial sync: `docker logs obsidian-vault-init`
- Follow periodic sync: `docker logs -f obsidian-vault-sync`
- Inspect mirrored files without the rclone entrypoint: `docker run --rm -v obsidian-knowledge-vault:/vault alpine find /vault -maxdepth 2 -type f`
- Follow MCP logs: `docker logs -f markdown-vault-mcp`
- Follow writer logs: `docker logs -f vault-writer-mcp`
- Trigger an immediate sync loop iteration: `docker restart obsidian-vault-sync`

## Editing Guidance
- If you add or rename environment variables in `compose.yaml`, update `.env.example` and the variable table in `README.md` in the same change.
- Keep security assumptions aligned with the current docs: no real `.env` in git, private repo, and do not expose port `8019` publicly without adding protection.
