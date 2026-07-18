# Obsidian Knowledge Infrastructure

A Portainer-ready Docker Compose stack that:

1. Lets Obsidian desktop/mobile clients edit the vault directly over WebDAV.
2. Keeps the local mirror refreshed on a schedule.
3. Indexes the mirrored Markdown vault.
4. Exposes the knowledge base through a read-only MCP endpoint.
5. Exposes a separate writer MCP that updates the source WebDAV vault safely for curator-style agents.
6. Exposes a thin context MCP (`curator-context-mcp`) that turns one curator question into a curated context object by calling the read-only reader once.

## Architecture

```text
Obsidian desktop/mobile clients
    |
    | WebDAV read/write
    v
NAS WebDAV (source of truth)
    |
    +--> vault-writer-mcp (Hermes write path)
    |
    | rclone sync
    v
Docker named volume: obsidian-knowledge-vault
    |
    +--> markdown-vault-mcp (Hermes read/search path)
            |
            +--> full-text index
            +--> semantic embeddings
            +--> MCP: http://HOST:8019/mcp
            |
            +--> curator-context-mcp (RAG-lite curated context)
                    |
                    +--> MCP: http://HOST:8021/mcp
```

The NAS WebDAV vault is the source of truth and is shared directly with your Obsidian clients over WebDAV. The Docker volume is only a disposable local mirror for indexing and read/search MCP access. Curator-style writes go through a separate writer MCP so agents do not edit the disposable mirror.

## Final Architecture

This project exists to give Hermes full knowledge access without making Hermes mount or edit the vault filesystem directly.

Final access model:

- Obsidian clients on desktop/mobile: direct WebDAV read/write to the NAS vault
- Hermes read/search/analysis: `markdown-vault-mcp` on `8019`, backed by the mirrored local volume
- Hermes curator writes: `vault-writer-mcp` on `8020`, backed by direct WebDAV access to the source vault
- Hermes curated context (RAG-lite): `curator-context-mcp` on `8021`, a thin read-only post-processor that calls the reader over MCP-HTTP and buckets the hits (heuristics, decisions, contradictions, MOCs, obsoletas) so the Curator gets one curated context object per question

That split is intentional:

- humans edit the source vault directly through WebDAV
- Hermes reads from the indexed mirror for fast semantic/context queries
- Hermes writes through a dedicated writer service so changes land in the source vault first
- the mirror catches up on the next sync cycle, and writer mutations now request an immediate sync trigger to reduce lag

## Services

### `vault-init`

Runs once during deployment and performs the initial WebDAV-to-local synchronization. The MCP service starts only after this synchronization succeeds.

### `vault-sync`

Periodically refreshes the local mirror using `rclone sync`.

This direction is intentionally one-way:

```text
NAS WebDAV -> Docker mirror
```

Any local file missing from WebDAV may be removed from the mirror. Do not treat the mirror as an editing location.

`vault-sync` also watches a small shared control volume for writer-triggered sync requests. After a successful writer mutation, the writer records a sync request and a per-path `changed-paths.log` entry so the mirror refreshes within a few seconds instead of waiting for the full polling interval.

When a writer request is detected, `vault-sync` first processes `changed-paths.log` (if any) by issuing one `rclone copyto` (or `deletefile`) per recorded path. This bypasses the WebDAV directory listing cache: the WebDAV may take several minutes to refresh a directory's `PROPFIND` listing after a write, but a direct GET on a specific file path returns the bytes immediately, so the mutated file lands in the mirror in under a second per path. Then a single `rclone sync` runs as best-effort cleanup for deletes and any other changes the writer did not announce. Scheduled syncs (every `SYNC_INTERVAL_SECONDS`) run a single pass as before.

### `markdown-vault-mcp`

Indexes the Markdown vault and exposes it using Streamable HTTP MCP. It is configured in application-level read-only mode.

The filesystem watcher is intentionally disabled (`MARKDOWN_VAULT_MCP_FILE_WATCHER=false`). The local mirror is populated by `vault-sync` from another container, and inotify does not see cross-container writes. Index convergence is handled by the boot-time reconciliation pass plus explicit `reindex` / `build_embeddings` calls from MCP.

### `vault-writer-mcp`

Writes Markdown notes directly to the source WebDAV vault through `rclone` commands backed by the same WebDAV credentials. It is intended for curator agents that need to update frontmatter, add links, move notes, and archive redundancies without writing into the disposable mirror.

### `curator-context-mcp`

A thin read-only MCP service (`http://HOST:8021/mcp`) that exposes a single tool, `consultar_contexto`, intended as the Curator's first call when tackling a task. For a given question it issues one hybrid `search` against the reader, then classifies each hit by its path into logical buckets (`Curator/heuristics`, `Curator/decisions`, `Curator/contradictions`, `MOCs/...`, `.curator-archive/...`) and downweights weak sources into a separate `obsoletas_o_baja_confianza` bucket, applies a 2x score boost to heuristics and MOCs, dedupes per path, and returns a single dict with `summary`, `mocs_relevantes`, `heuristicas`, `decisiones`, `contradicciones`, `obsoletas_o_baja_confianza`, and `metricas`. It never invokes an LLM and never invents content; the `summary` field is a deterministic digest (hit counts + top path + bucket breakdown). `perfil_origen` is recorded only for traceability in `metricas`. It mounts no volumes: all vault access is via MCP-HTTP to `markdown-vault-mcp`.

## Hermes Profiles

- `hermes/knowledge-curator.md`: repo-local prompt/instructions for a Hermes curator profile that knows how to use both MCP services safely

Use that file as the source of truth for the Hermes `Knowledge Curator` system prompt instead of maintaining an unrelated copy elsewhere.

## Requirements

- Docker Engine with Compose support
- Portainer capable of deploying a stack from a Git repository
- A WebDAV endpoint on the NAS
- Network access from the Docker host to the NAS
- Network access from Hermes to TCP port `8019` on this host
- Network access from Hermes curator agents to TCP port `8020` on this host when write access is needed

## Repository setup

Create a private GitHub repository and add these files:

```text
.
├── compose.yaml
├── .env.example
├── .gitignore
└── README.md
```

Do not commit a real `.env` file or credentials.

## Generate the WebDAV password value

The rclone WebDAV backend expects an obscured password value:

```bash
docker run --rm rclone/rclone:latest obscure 'YOUR_REAL_PASSWORD'
```

Copy the output into the Portainer environment variable:

```text
WEBDAV_PASSWORD_OBSCURED
```

Obscuring is not encryption. Protect the Portainer account and Docker host.

## Choose the right WebDAV URL shape

`WEBDAV_URL` can point to either:

- the WebDAV base endpoint, with `WEBDAV_REMOTE_PATH` set to the vault path below it
- the vault root itself, with `WEBDAV_REMOTE_PATH` left empty

Examples:

```text
WEBDAV_URL=https://nas.example.net:5006/
WEBDAV_REMOTE_PATH=home/OBS_VAULT
```

```text
WEBDAV_URL=https://192.168.31.150:5006/home/OBS_VAULT/
WEBDAV_REMOTE_PATH=
```

Some NAS WebDAV setups use self-signed certificates or certificates that do not validate for the IP address used in `WEBDAV_URL`. In that case, set:

```text
WEBDAV_NO_CHECK_CERTIFICATE=true
```

Prefer a hostname with a matching certificate when possible. `WEBDAV_NO_CHECK_CERTIFICATE=true` is a compatibility fallback.

## Deploy from Portainer

1. Push this project to a private GitHub repository.
2. In Portainer, open **Stacks**.
3. Select **Add stack**.
4. Choose **Git repository**.
5. Enter the repository URL and credentials when private.
6. Set the Compose path to:

```text
compose.yaml
```

7. Add all variables listed in `.env.example` under the stack environment variables.
8. Deploy the stack.

Portainer clones the repository when deploying a Git-backed stack. GitOps updates can later pull and redeploy changes from the repository.

### Optional GitHub Actions redeploy

This repo includes `.github/workflows/redeploy-portainer.yml`, which can trigger a Portainer stack redeploy automatically on every push to `master`.

To enable it, add this GitHub Actions secret in the repository settings:

```text
PORTAINER_WEBHOOK_URL
```

Set it to the full Portainer stack webhook URL. Keep it in GitHub Secrets, not in the committed workflow file.

## Required Portainer variables

| Variable | Example |
|---|---|
| `WEBDAV_URL` | `https://nas.example.net/webdav/` |
| `WEBDAV_REMOTE_PATH` | `Obsidian/MyVault` |
| `WEBDAV_VENDOR` | `other` |
| `WEBDAV_USERNAME` | `osvi` |
| `WEBDAV_PASSWORD_OBSCURED` | output of `rclone obscure` |
| `WEBDAV_NO_CHECK_CERTIFICATE` | `false` |
| `SYNC_INTERVAL_SECONDS` | `300` |
| `CURATOR_ARCHIVE_ROOT` | `.curator-archive` |
| `CURATOR_ALLOW_HARD_DELETE` | `false` |
| `PUID` | `1000` |
| `PGID` | `1000` |

## Verify synchronization

Inspect the initial synchronization:

```bash
docker logs obsidian-vault-init
```

Inspect the periodic synchronization:

```bash
docker logs -f obsidian-vault-sync
```

List the mirrored vault:

```bash
docker exec obsidian-vault-sync rclone lsf /vault
```

Because the rclone image uses an `rclone` entrypoint by default, an easier generic inspection command is:

```bash
docker run --rm -v obsidian-knowledge-vault:/vault alpine find /vault -maxdepth 2 -type f
```

## Verify MCP

Inspect MCP logs:

```bash
docker logs -f markdown-vault-mcp
```

Look for messages indicating:

- documents indexed
- chunks generated
- embeddings saved
- Uvicorn listening on `0.0.0.0:8000`

The external endpoint is:

```text
http://DOCKER_HOST_IP:8019/mcp
```

The writer endpoint is:

```text
http://DOCKER_HOST_IP:8020/mcp
```

The curator context endpoint is:

```text
http://DOCKER_HOST_IP:8021/mcp
```

Use the Docker host's LAN IP from another container or machine. Do not use `localhost` from Hermes when Hermes runs on another host.

## Connect Hermes

Configure a remote MCP server in the Hermes dashboard:

```text
Name: obsidian-knowledge
Transport: Streamable HTTP
URL: http://DOCKER_HOST_IP:8019/mcp
```

No authentication is configured in this baseline stack. Keep the endpoint restricted to a trusted LAN or VPN.

For a curator-capable Hermes setup, register both remote MCP servers:

```text
Name: obsidian-knowledge
Transport: Streamable HTTP
URL: http://DOCKER_HOST_IP:8019/mcp
```

```text
Name: vault-writer-mcp
Transport: Streamable HTTP
URL: http://DOCKER_HOST_IP:8020/mcp
```

```text
Name: curator-context-mcp
Transport: Streamable HTTP
URL: http://DOCKER_HOST_IP:8021/mcp
```

Recommended role split inside Hermes:

- `obsidian-knowledge`: search, read, backlinks, semantic discovery, context gathering
- `vault-writer-mcp`: write, move, archive, frontmatter updates, link insertion
- `curator-context-mcp`: one-shot curated context per question (`consultar_contexto`)

## Curator Writer MCP

Use the two MCP endpoints for different jobs:

- `http://DOCKER_HOST_IP:8019/mcp`: read/search/index endpoint backed by the local mirror
- `http://DOCKER_HOST_IP:8020/mcp`: write endpoint backed by direct WebDAV access
- `http://DOCKER_HOST_IP:8021/mcp`: curated context endpoint (`consultar_contexto`) backed by MCP-HTTP to the reader

The writer MCP currently exposes note-focused tools for safe curation work:

- `read_note`
- `write_note`
- `upsert_frontmatter`
- `append_links`
- `move_note`
- `archive_note`
- `delete_note`
- `list_folder`
- `stat_path`
- `request_sync`

Safety model:

- writer tools operate on `.md` notes only
- note paths are always relative to the vault root
- `read_note` returns a `sha256` token; pass it back as `expected_sha256` on edits to avoid overwriting concurrent changes
- `delete_note` archives by default instead of hard-deleting
- hard delete stays disabled unless `CURATOR_ALLOW_HARD_DELETE=true`
- successful write, move, archive, and delete operations also request an immediate mirror sync
- `request_sync` is the only MCP way to force the mirror to refresh without performing a writer mutation (useful after a human edits the vault directly through NAS WebDAV)

Recommended curator workflow:

1. Discover candidate notes with the read-only MCP on `8019`
2. Read target notes with the writer MCP to obtain fresh `sha256` values
3. Apply localized changes such as frontmatter updates, link insertion, moves, or archival
4. Wait a few seconds for the writer-triggered sync request to refresh the mirror, or call `request_sync` (writer MCP) followed by `reindex` or `build_embeddings` (read-only MCP) if you need a faster end-to-end refresh
5. Re-query the read-only MCP to validate the new knowledge graph state

## Updating the vault

The synchronization interval is controlled by:

```text
SYNC_INTERVAL_SECONDS
```

The file watcher in `markdown-vault-mcp` detects changes inside the local mirror and updates its indexes.

In normal operation, curator writes through `vault-writer-mcp` request an immediate mirror refresh automatically. The remaining lag is usually the time for `vault-sync` to run the triggered sync and for `markdown-vault-mcp` to notice the new files inside the mirror.

### On-demand refresh from MCP

Agents and humans can force a refresh without restarting containers by combining the two MCPs:

1. Call `request_sync` on the writer MCP (`8020/mcp`) to drop a sync request into the shared `sync-control` volume. `vault-sync` picks it up on its next loop iteration (within a second) and runs the per-path `copyto` cleanup.
2. Call `reindex` on the read-only MCP (`8019/mcp`) to force a full vault reindex immediately. The reader's filesystem watcher is disabled by design (the mirror is populated by another container), so `reindex` is the only way to pick up external changes once the mirror is fresh. Use `build_embeddings` if you only need to refresh the vector index, and `get_index_status` to verify the state.

If `request_sync` is called without a preceding writer mutation (e.g. a human edited the vault directly through NAS WebDAV), there is no per-path entry in `changed-paths.log`; the mirror then depends on the single cleanup `rclone sync`, which is subject to the WebDAV directory listing propagation delay (typically a few minutes). In that case, a `docker restart obsidian-vault-sync` is still the heavy hammer.

This is the recommended path after a human edits the vault directly through NAS WebDAV and a curator agent wants to see the changes without performing any writer mutation.

To trigger a synchronization immediately from the Docker host:

```bash
docker restart obsidian-vault-sync
```

Restarting begins the loop with an immediate sync.

## Updating the infrastructure

Update `compose.yaml` in GitHub, then use Portainer's Git stack update/redeploy function. You may also enable Portainer GitOps updates according to your Portainer edition and configuration.

## Persistence

Two named volumes are created:

```text
obsidian-knowledge-vault
obsidian-knowledge-mcp-state
```

- `obsidian-knowledge-vault`: local mirror of the NAS vault
- `obsidian-knowledge-mcp-state`: SQLite index, vectors, and cache

Deleting the MCP state volume is safe but forces a complete reindex. Deleting the vault mirror is also recoverable from WebDAV, but the next initial sync will be required.

## Security

- Keep the Git repository private.
- Never commit WebDAV credentials.
- Do not publish port `8019` directly to the Internet.
- Do not publish port `8020` directly to the Internet.
- Prefer Tailscale, WireGuard, or a protected reverse proxy for remote access.
- Keep the reader MCP on `8019` in read-only mode. Treat the writer MCP on `8020` as a privileged curator path.
- Give the NAS WebDAV account access only to the vault directory.
- Back up the NAS vault independently; synchronization is not a backup.

## Important behavior

`rclone sync` makes the destination match the source. Files deleted remotely are deleted from the local mirror. Internal MCP state stored under `.markdown_vault_mcp` is excluded from synchronization, and the main index is kept in the separate `mcp-state` volume.

The writer MCP updates WebDAV directly, so curator changes become the new source of truth first and then flow back into the local mirror on the next sync.

That means the system is still eventually consistent, but the shared sync trigger reduces the gap between writer-visible changes and reader-visible changes.

This stack builds the rclone remote entirely from environment variables. The remote name in `compose.yaml` is `naswebdav`, so related `RCLONE_CONFIG_...` variables must use that exact name.

## Optional next improvements

- Add MCP authentication or protect it behind a reverse proxy.
- Switch FastEmbed to the multilingual `bge-m3` model through Ollama.
- Add health monitoring and notifications for failed WebDAV synchronization.
- Pin container images to immutable versions or digests.
