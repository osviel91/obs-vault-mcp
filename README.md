# Obsidian Knowledge Infrastructure

See `CHANGELOG.md` for feature and fix history.

A Portainer-ready Docker Compose stack that:

1. Lets Obsidian desktop/mobile clients edit the vault directly over WebDAV.
2. Keeps the local mirror refreshed on a schedule.
3. Indexes the mirrored Markdown vault.
4. Extracts non-Markdown vault documents into mirror-local Markdown shadow notes (with optional PDF OCR) so the reader can index them too.
5. Exposes the knowledge base through a read-only MCP endpoint.
6. Exposes a separate writer MCP that updates the source WebDAV vault safely for curator-style agents.
7. Exposes a thin context MCP (`curator-context-mcp`) that turns one curator question into a curated context object by calling the read-only reader once.

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
    +--> vault-ingest (/vault/.ingest shadow notes, optional PDF OCR)
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
- Hermes direct read/search/analysis: `markdown-vault-mcp` on `8019`, backed by the mirrored local volume
- Hermes document extraction for PDFs/docx/etc.: `vault-ingest`, which writes mirror-local Markdown shadow notes under `/.ingest` so the reader can index non-Markdown content too
- Hermes curator writes: `vault-writer-mcp` on `8020`, backed by direct WebDAV access to the source vault
- Hermes curated context (RAG-lite, recommended first step for knowledge questions): `curator-context-mcp` on `8021`, a thin read-only post-processor that calls the reader over MCP-HTTP and buckets the hits (heuristics, decisions, contradictions, MOCs, obsolete/low-confidence material) so the Curator gets one curated context object per question

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

Indexes the Markdown vault and exposes it using Streamable HTTP MCP. It is configured in application-level read-only mode and is the general-purpose reader MCP for direct vault inspection. See `MCP Endpoints And Tools` below for the recommended query flow and tool categories.

The filesystem watcher is intentionally disabled (`MARKDOWN_VAULT_MCP_FILE_WATCHER=false`). The local mirror is populated by `vault-sync` from another container, and inotify does not see cross-container writes. Index convergence is handled by the boot-time reconciliation pass plus explicit `reindex` / `build_embeddings` calls from MCP.

### `vault-writer-mcp`

Writes Markdown notes directly to the source WebDAV vault through `rclone` commands backed by the same WebDAV credentials. It is the safe write path for curator agents and never writes into the disposable mirror. See `MCP Endpoints And Tools` below for the write tool groups and safety rules.

### `vault-ingest`

Scans the mirrored vault for non-Markdown documents (`.pdf`, `.docx`, `.pptx`, `.xlsx`, `.html`, `.csv`, `.json`, `.txt`, `.doc`) and writes extracted Markdown shadow notes under `/.ingest` inside the mirror. Those generated files are excluded from `rclone sync` so they stay mirror-local and do not pollute the source WebDAV vault.

For PDFs, `vault-ingest` can run `ocrmypdf` first (`OCR_PDFS=true`) so scanned/image-only PDFs become searchable too. Each shadow note stores `source_path`, `source_sha256`, `source_mtime`, `ingest_kind: shadow`, and `ocr_applied` in frontmatter so agents can trace the extracted text back to the original document.

### `curator-context-mcp`

A thin read-only MCP service (`http://HOST:8021/mcp`) that exposes a single tool, `consultar_contexto`. It is the recommended first MCP for knowledge questions: it performs one hybrid search against the reader, classifies the results into curator-friendly buckets, and returns a deterministic context object without using an LLM. It mounts no volumes: all vault access is via MCP-HTTP to `markdown-vault-mcp`. See `MCP Endpoints And Tools` below for the exact behavior and response fields.

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
- Network access from Hermes curator agents to TCP port `8021` on this host for curated context queries

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
| `INGEST_INTERVAL_SECONDS` | `600` |
| `OCR_PDFS` | `true` |
| `OCR_LANGS` | `spa+eng` |
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

Inspect ingest logs:

```bash
docker logs -f obsidian-vault-ingest
```

Look for messages indicating:

- shadow notes written under `/.ingest/...`
- OCR applied to scanned PDFs when `OCR_PDFS=true`

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

## MCP Endpoints And Tools

This stack exposes three MCP servers with intentionally different responsibilities. Keeping that split clear is the easiest way to avoid accidental writes to the disposable mirror or unnecessary direct reads from WebDAV.

| MCP | Endpoint | Purpose | Writes to source vault |
|---|---|---|---|
| `markdown-vault-mcp` | `http://DOCKER_HOST_IP:8019/mcp` | Indexed read/search interface over the mirrored vault | No |
| `vault-writer-mcp` | `http://DOCKER_HOST_IP:8020/mcp` | Safe curator write interface over WebDAV | Yes |
| `curator-context-mcp` | `http://DOCKER_HOST_IP:8021/mcp` | Deterministic curated context per question | No |

### Which MCP Should I Use To Query Knowledge?

Use `curator-context-mcp` first when the goal is to ask, "What do we know about X?" It is the recommended entry point for knowledge queries because it turns one question into a curated context object instead of returning a flat list of raw matches.

Use `markdown-vault-mcp` after that when you need to inspect the vault directly: run broader searches, read full notes or sections, inspect backlinks, check similar notes, or validate the source material behind the curated context.

Do not use `vault-writer-mcp` as the primary knowledge-query interface. Its job is safe mutation of the source vault. The main exception is reading a note with `read_note` immediately before editing it so you can obtain the current `sha256` for optimistic concurrency.

Recommended query flow:

```text
User question
    -> curator-context-mcp / consultar_contexto
    -> if more detail is needed: markdown-vault-mcp / search, read, get_backlinks, get_similar
    -> if a change is approved: vault-writer-mcp
```

### `markdown-vault-mcp` (`8019`)

This is the main reader MCP. In this stack it runs with `MARKDOWN_VAULT_MCP_READ_ONLY=true`, so upstream write tools stay hidden and only the read/search/index-management surface is exposed. Think of it as the raw vault interface: powerful and general-purpose, but not opinionated about which notes matter most for a curator question.

What it is for:

- searching the mirrored vault by keywords, semantics, or hybrid ranking
- reading full notes or specific sections
- exploring links, backlinks, similar notes, recent notes, and orphan notes
- checking whether the index and embeddings are ready
- forcing reindexing after mirror changes because the file watcher is intentionally disabled in this deployment

Key tools normally available in this deployment:

- Discovery and reading: `search`, `read`, `list_documents`, `list_folders`, `list_tags`, `stats`
- Link graph and navigation: `get_backlinks`, `get_outlinks`, `get_broken_links`, `get_similar`, `get_toc`, `get_recent`, `get_context`, `get_orphan_notes`, `get_most_linked`, `get_connection_path`
- Index and embeddings: `reindex`, `build_embeddings`, `get_index_status`, `embeddings_status`

Operational notes:

- It reads from the local mirror volume, not from WebDAV directly.
- Because `MARKDOWN_VAULT_MCP_FILE_WATCHER=false`, external changes only become queryable after the mirror is refreshed and the reader is reindexed.
- If a curator agent needs to modify anything, it must switch to `vault-writer-mcp` instead of trying to write here.

### `vault-writer-mcp` (`8020`)

This MCP is the write path for curator-style agents. It talks directly to the source WebDAV vault through `rclone`, so successful changes land in the real vault first and then request a faster mirror refresh.

What it is for:

- reading a target Markdown note before editing it
- updating note content or YAML frontmatter safely
- appending semantic links without duplicating them
- moving, archiving, or deleting notes while carrying sibling `NoteName_assets/` folders with them
- managing local note assets and other non-Markdown files without touching internal stack paths
- requesting an on-demand mirror refresh when a human changed the vault directly through WebDAV

Tools exposed by this MCP:

- Inspection: `read_note`, `stat_path`, `list_folder`
- Markdown note editing: `write_note`, `upsert_frontmatter`, `append_links`
- Note structure changes: `move_note`, `archive_note`, `delete_note`
- Note asset management (`NoteName_assets/` only): `organize_note_assets`, `move_asset`, `archive_asset`, `delete_asset`
- Generic non-Markdown file management: `move_file`, `archive_file`, `delete_file`
- Sync trigger: `request_sync`

Safety model:

- Content-editing tools only work on `.md` notes.
- `read_note` returns a `sha256`; pass it back as `expected_sha256` when editing to avoid overwriting concurrent changes.
- Asset lifecycle tools are limited to paths inside a single `NoteName_assets/` owner folder.
- Generic file lifecycle tools reject internal stack paths like `/.ingest`, `/.markdown_vault_mcp`, `.obsidian`, `.trash`, `.git`, and `.webdav-sync-ready`.
- `delete_note`, `delete_asset`, and `delete_file` archive by default; hard delete stays disabled unless `CURATOR_ALLOW_HARD_DELETE=true`.
- Successful write, move, archive, and delete operations also drop a sync request so `vault-sync` refreshes the mirror quickly.

### `curator-context-mcp` (`8021`)

This MCP is intentionally narrow: it exposes a single tool, `consultar_contexto`, as a deterministic first-pass context builder for curator work. This is the recommended MCP to use first when the user is asking for knowledge, prior decisions, heuristics, contradictions, or relevant MOCs about a topic.

What it is for:

- taking one curator question and turning it into a structured context object
- prioritizing heuristics and MOCs over weaker matches
- separating decisions, contradictions, and obsolete or low-confidence material before the curator reads full notes
- giving the curator an honest "not enough context" style answer instead of inventing content

Tool exposed by this MCP:

- `consultar_contexto(question, perfil_origen="", max_heuristicas=5, max_contradicciones=3, umbral_similitud=0.6)`

What `consultar_contexto` returns:

The field names below are the literal response keys returned by the MCP:

- `summary`
- `mocs_relevantes`
- `heuristicas`
- `decisiones`
- `contradicciones`
- `obsoletas_o_baja_confianza`
- `metricas`

Behavior notes:

- It makes exactly one hybrid `search` call against `markdown-vault-mcp` and then post-processes the hits.
- It classifies by path conventions such as `Curator/heuristics`, `Curator/decisions`, `Curator/contradictions`, `MOCs/...`, and `.curator-archive/...`.
- It discards inbox-style material like `Curator/inbox/**` and never invents summaries with an LLM.
- It boosts heuristics and MOCs, relegates obsolete or low-confidence snippets to a weaker bucket, dedupes by path, and reports traceability data in `metricas`.

## Curator Workflow By MCP

Use the three MCP endpoints for different jobs:

- `http://DOCKER_HOST_IP:8019/mcp`: read/search/index endpoint backed by the local mirror
- `http://DOCKER_HOST_IP:8020/mcp`: write endpoint backed by direct WebDAV access
- `http://DOCKER_HOST_IP:8021/mcp`: curated context endpoint (`consultar_contexto`) backed by MCP-HTTP to the reader

Recommended curator workflow:

1. Start with `consultar_contexto` on `curator-context-mcp` (`8021`) to get a curated first-pass answer to the question
2. Use `markdown-vault-mcp` (`8019`) for any deeper inspection: broader search, full-note reads, backlinks, similar notes, or source validation
3. Read target notes with `vault-writer-mcp` (`8020`) to obtain fresh `sha256` values before editing
4. Apply localized changes such as frontmatter updates, link insertion, moves, or archival
5. Wait a few seconds for the writer-triggered sync request to refresh the mirror, or call `request_sync` (writer MCP) followed by `reindex` or `build_embeddings` (read-only MCP) if you need a faster end-to-end refresh
6. Re-query `curator-context-mcp` for the curated view or `markdown-vault-mcp` for direct source validation

## Updating the vault

The synchronization interval is controlled by:

```text
SYNC_INTERVAL_SECONDS
```

`vault-ingest` scans non-Markdown files on its own interval:

```text
INGEST_INTERVAL_SECONDS
```

It writes generated Markdown shadow notes under `/.ingest` inside the mirror. Because the reader's file watcher is disabled by design, those shadow notes still require an explicit `reindex` (or `build_embeddings`) before they become queryable through MCP.

In normal operation, curator writes through `vault-writer-mcp` request an immediate mirror refresh automatically. The remaining lag is usually the time for `vault-sync` to run the triggered sync, for `vault-ingest` to notice any changed non-Markdown documents, and for `markdown-vault-mcp` to be reindexed.

### On-demand refresh from MCP

Agents and humans can force a refresh without restarting containers by combining the two MCPs:

1. Call `request_sync` on the writer MCP (`8020/mcp`) to drop a sync request into the shared `sync-control` volume. `vault-sync` picks it up on its next loop iteration (within a second) and runs the per-path `copyto` cleanup.
2. Wait for the next `vault-ingest` pass if the changed content is a PDF/docx/etc. that must be extracted into `/.ingest` first.
3. Call `reindex` on the read-only MCP (`8019/mcp`) to force a full vault reindex immediately. The reader's filesystem watcher is disabled by design (the mirror is populated by another container), so `reindex` is the only way to pick up external changes once the mirror is fresh. Use `build_embeddings` if you only need to refresh the vector index, and `get_index_status` to verify the state.

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

Three named volumes are created:

```text
obsidian-knowledge-vault
obsidian-knowledge-mcp-state
obsidian-knowledge-sync-control
```

- `obsidian-knowledge-vault`: local mirror of the NAS vault
- `obsidian-knowledge-mcp-state`: SQLite index, vectors, and cache
- `obsidian-knowledge-sync-control`: sync trigger handoff between writer and sync containers

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

`rclone sync` makes the destination match the source. Files deleted remotely are deleted from the local mirror. Generated shadow notes stored under `/.ingest` and internal MCP state stored under `.markdown_vault_mcp` are excluded from synchronization, and the main index is kept in the separate `mcp-state` volume.

The writer MCP updates WebDAV directly, so curator changes become the new source of truth first and then flow back into the local mirror on the next sync.

That means the system is still eventually consistent, but the shared sync trigger reduces the gap between writer-visible changes and reader-visible changes.

This stack builds the rclone remote entirely from environment variables. The remote name in `compose.yaml` is `naswebdav`, so related `RCLONE_CONFIG_...` variables must use that exact name.

## Optional next improvements

- Add MCP authentication or protect it behind a reverse proxy.
- Switch FastEmbed to the multilingual `bge-m3` model through Ollama.
- Add health monitoring and notifications for failed WebDAV synchronization.
- Pin container images to immutable versions or digests.
