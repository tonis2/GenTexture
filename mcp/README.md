# GenTexture MCP

Call GenTexture's image-generation APIs (Stability, fal · FLUX, fal · FLUX
General, fal · Nano Banana, Google · Gemini direct, local FLUX server)
from an external AI agent like Claude Code or OpenCode.

## Architecture

```
Claude Code / OpenCode  ──stdio──►  gentex-mcp-server.py  ──TCP──►  Blender (GenTexture addon)
       (MCP client)                   (this directory)              listening on 127.0.0.1:9877
```

API keys never leave Blender. The MCP server is a thin bridge: it
translates each MCP tool call into a TCP JSON command for the in-Blender
server hosted by the addon.

## Setup

### 1. Blender side

1. Make sure the GenTexture addon is enabled and your provider API keys
   are filled in (Edit → Preferences → Add-ons → GenTexture).
2. In the same preferences panel, scroll to **MCP Server**:
   - check **Enable MCP Server**
   - leave host = `127.0.0.1`, port = `9877` (or pick another port)
   - click **Start**
3. The status line should change to `Running on 127.0.0.1:9877`.
4. Save preferences. Next time Blender launches, the server auto-starts.

### 2. Client side — install the `mcp` package

```bash
pip install -r /abs/path/to/GenTexture/mcp/requirements.txt
```

This installs the official `mcp` Python SDK into whatever Python your MCP
client will launch the bridge with.

### 3. Register the bridge with your MCP client

#### Claude Code

Copy [`claude.json.example`](claude.json.example) into your Claude
config (e.g. `~/.claude.json` or project-local `.mcp.json`), or run:

```bash
claude mcp add gentex -- python3 /abs/path/to/GenTexture/mcp/gentex-mcp-server.py
```

…then add the env vars `BLENDER_HOST` and `BLENDER_PORT` if they differ
from the defaults.

#### OpenCode

Copy [`opencode.json.example`](opencode.json.example) into your project's
`opencode.json`. OpenCode's MCP config differs from Claude's in three
small ways:

| Field           | Claude              | OpenCode              |
|-----------------|---------------------|-----------------------|
| Top-level key   | `mcpServers`        | `mcp`                 |
| Command shape   | `command` + `args`  | `command` (array)     |
| Env key         | `env`               | `environment`         |
| Requires        | —                   | `"type": "local"`, `"enabled": true` |

The MCP server script itself is identical for both — only the client-side
JSON differs.

## Available tools

| Tool | What it does |
|---|---|
| `status` | Health check. Always call first if other tools start failing. |
| `list_providers` | Lists provider ids, capabilities, and whether each API key is configured. |
| `text2img` | Prompt → image. Stored in `bpy.data.images[name]` + returned inline. |
| `img2img` | Init image + prompt → image. |
| `inpaint` | Init + mask + prompt → image. White mask = regenerate. |
| `list_images` | List Blender's loaded images (optional name filter). |
| `get_image` | Fetch an image as base64 PNG so the agent can see it. |
| `save_image_to_file` | Persist a Blender image to disk. |
| `import_image_file` | Load a PNG/JPEG into `bpy.data.images`. |
| `bake_layers` | Composite a mesh's GenTexture layer stack into a single texture. |

Image-shaped arguments (`init_image`, `mask_image`, `depth_image`,
`reference_images`) accept **either** a `bpy.data.images` datablock name
**or** a base64-encoded PNG.

## Troubleshooting

- **"Cannot reach Blender at 127.0.0.1:9877"** — Blender isn't running,
  the addon is disabled, or the MCP server toggle is off.
- **"Bind failed: address already in use"** — another GenTexture session,
  the standalone `blender-mcp` addon (default port 9876), or another
  service is using the port. Change `mcp_port` in addon prefs.
- **`ModuleNotFoundError: No module named 'mcp'`** — install with
  `pip install mcp` in the Python that launches `gentex-mcp-server.py`.
- **Concurrent generation calls hang** — each provider serializes
  internally because its status-file path is fixed. Different providers
  run in parallel.
