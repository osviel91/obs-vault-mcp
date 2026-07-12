# Obsidian Knowledge Infrastructure

A Portainer-ready Docker Compose stack that:

1. Mirrors an Obsidian vault from a NAS over WebDAV.
2. Keeps the local mirror refreshed on a schedule.
3. Indexes the mirrored Markdown vault.
4. Exposes the knowledge base through a read-only MCP endpoint.

## Architecture

```text
NAS WebDAV
    |
    | rclone sync
    v
Docker named volume: obsidian-knowledge-vault
    |
    +--> markdown-vault-mcp
            |
            +--> full-text index
            +--> semantic embeddings
            +--> MCP: http://HOST:8019/mcp
```

The NAS WebDAV vault is the source of truth. The Docker volume is a disposable local mirror. The MCP index and embeddings are stored in a separate persistent volume.

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

### `markdown-vault-mcp`

Indexes the Markdown vault and exposes it using Streamable HTTP MCP. It is configured in application-level read-only mode.

## Requirements

- Docker Engine with Compose support
- Portainer capable of deploying a stack from a Git repository
- A WebDAV endpoint on the NAS
- Network access from the Docker host to the NAS
- Network access from Hermes to TCP port `8019` on this host

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

Use the Docker host's LAN IP from another container or machine. Do not use `localhost` from Hermes when Hermes runs on another host.

## Connect Hermes

Configure a remote MCP server in the Hermes dashboard:

```text
Name: obsidian-knowledge
Transport: Streamable HTTP
URL: http://DOCKER_HOST_IP:8019/mcp
```

No authentication is configured in this baseline stack. Keep the endpoint restricted to a trusted LAN or VPN.

## Updating the vault

The synchronization interval is controlled by:

```text
SYNC_INTERVAL_SECONDS
```

The file watcher in `markdown-vault-mcp` detects changes inside the local mirror and updates its indexes.

To trigger a synchronization immediately:

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
- Prefer Tailscale, WireGuard, or a protected reverse proxy for remote access.
- Keep MCP in read-only mode until you deliberately design a reviewed write workflow.
- Give the NAS WebDAV account access only to the vault directory.
- Back up the NAS vault independently; synchronization is not a backup.

## Important behavior

`rclone sync` makes the destination match the source. Files deleted remotely are deleted from the local mirror. Internal MCP state stored under `.markdown_vault_mcp` is excluded from synchronization, and the main index is kept in the separate `mcp-state` volume.

This stack builds the rclone remote entirely from environment variables. The remote name in `compose.yaml` is `naswebdav`, so related `RCLONE_CONFIG_...` variables must use that exact name.

## Optional next improvements

- Add MCP authentication or protect it behind a reverse proxy.
- Switch FastEmbed to the multilingual `bge-m3` model through Ollama.
- Add health monitoring and notifications for failed WebDAV synchronization.
- Pin container images to immutable versions or digests.
