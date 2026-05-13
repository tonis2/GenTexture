#!/usr/bin/env python3
"""GenTexture MCP server.

A standalone stdio MCP server. Forwards each MCP tool call to a TCP JSON
command on the in-Blender GenTexture addon (which hosts the actual
generation logic and holds the API keys).

Install:

    pip install mcp

Run via your MCP client:

    Claude Code (~/.claude.json or .mcp.json):
      {
        "mcpServers": {
          "gentex": {
            "command": "python3",
            "args": ["/abs/path/to/GenTexture/mcp/gentex-mcp-server.py"],
            "env": {"BLENDER_HOST": "127.0.0.1", "BLENDER_PORT": "9877"}
          }
        }
      }

    OpenCode (opencode.json):
      {
        "$schema": "https://opencode.ai/config.json",
        "mcp": {
          "gentex": {
            "type": "local",
            "command": ["python3", "/abs/path/to/GenTexture/mcp/gentex-mcp-server.py"],
            "environment": {"BLENDER_HOST": "127.0.0.1", "BLENDER_PORT": "9877"},
            "enabled": true
          }
        }
      }

The addon must be enabled in Blender AND the MCP server toggled on in
GenTexture preferences (or auto-started after a prior enable).
"""

import asyncio
import json
import os
import socket
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    Tool,
    TextContent,
    ImageContent,
)


BLENDER_HOST = os.environ.get("BLENDER_HOST", "127.0.0.1")
BLENDER_PORT = int(os.environ.get("BLENDER_PORT", "9877"))
SOCK_TIMEOUT = float(os.environ.get("GENTEX_TIMEOUT", "600"))


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

def _tool_list() -> list[Tool]:
    return [
        Tool(
            name="status",
            description=(
                "Health check. Returns {ok: true, providers: [...]} when the "
                "in-Blender server is reachable. Call this first if other "
                "tools start failing — distinguishes 'Blender not running' "
                "from 'this prompt was bad'."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="list_providers",
            description=(
                "List image-generation providers configured in the Blender "
                "GenTexture addon. Returns each provider's id, label, "
                "capabilities (text2img / img2img / inpaint / "
                "reference_images / depth_control), and whether its API key "
                "is set. Use the returned id with text2img/img2img/inpaint."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="text2img",
            description=(
                "Generate an image from a text prompt via the named provider. "
                "Result is stored as `bpy.data.images[image_name]` inside "
                "Blender and ALSO returned inline as base64 PNG so you can "
                "see it."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "provider": {"type": "string",
                                 "description": "Provider id from list_providers"},
                    "prompt": {"type": "string"},
                    "negative_prompt": {"type": "string", "default": ""},
                    "width": {"type": "integer", "default": 1024},
                    "height": {"type": "integer", "default": 1024},
                    "seed": {"type": "integer", "description": "Optional"},
                    "save_as": {"type": "string",
                                "description": "Datablock name in bpy.data.images. "
                                               "If omitted, auto-generated from prompt."},
                    "reference_images": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: list of bpy.data.images names OR "
                                       "base64-encoded PNGs (provider-dependent).",
                    },
                },
                "required": ["provider", "prompt"],
            },
        ),
        Tool(
            name="img2img",
            description=(
                "Generate an image conditioned on an existing image. The "
                "init_image arg is either a bpy.data.images name OR a "
                "base64-encoded PNG."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "provider": {"type": "string"},
                    "prompt": {"type": "string"},
                    "init_image": {"type": "string",
                                   "description": "bpy.data.images name or base64 PNG"},
                    "negative_prompt": {"type": "string", "default": ""},
                    "width": {"type": "integer", "default": 1024},
                    "height": {"type": "integer", "default": 1024},
                    "strength": {"type": "number", "default": 0.75,
                                 "description": "0=keep init, 1=ignore init"},
                    "depth_image": {"type": "string",
                                    "description": "Optional ControlNet depth map"},
                    "depth_scale": {"type": "number", "default": 0.6},
                    "seed": {"type": "integer"},
                    "save_as": {"type": "string"},
                    "reference_images": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["provider", "prompt", "init_image"],
            },
        ),
        Tool(
            name="inpaint",
            description=(
                "Regenerate masked region of an existing image. Mask: white "
                "= region to regenerate, black = keep. Both image args are "
                "bpy.data.images names OR base64 PNG."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "provider": {"type": "string"},
                    "prompt": {"type": "string"},
                    "init_image": {"type": "string"},
                    "mask_image": {"type": "string"},
                    "negative_prompt": {"type": "string", "default": ""},
                    "width": {"type": "integer", "default": 1024},
                    "height": {"type": "integer", "default": 1024},
                    "strength": {"type": "number", "default": 0.75},
                    "depth_image": {"type": "string"},
                    "depth_scale": {"type": "number", "default": 0.6},
                    "seed": {"type": "integer"},
                    "save_as": {"type": "string"},
                },
                "required": ["provider", "prompt", "init_image", "mask_image"],
            },
        ),
        Tool(
            name="list_images",
            description=(
                "List images currently loaded in Blender (bpy.data.images). "
                "Optional name substring filter."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {"type": "string", "default": ""},
                },
                "required": [],
            },
        ),
        Tool(
            name="get_image",
            description=(
                "Fetch a bpy.data.images datablock as base64 PNG so the "
                "agent can see it. Use max_size to bound the largest side."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "max_size": {"type": "integer", "default": 1024,
                                 "description": "Cap the larger dimension to this many pixels"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="save_image_to_file",
            description=(
                "Write a Blender image to a PNG file on disk. The path must "
                "be writable from Blender's process."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "bpy.data.images datablock name"},
                    "path": {"type": "string",
                             "description": "Absolute filesystem path ending in .png"},
                },
                "required": ["name", "path"],
            },
        ),
        Tool(
            name="import_image_file",
            description=(
                "Load an image from disk into bpy.data.images. Optional "
                "save_as renames the datablock."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "save_as": {"type": "string"},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="bake_layers",
            description=(
                "Bake the projected GenTexture layer stack of a Blender "
                "object into a single composite image. The object must have "
                "at least one entry in obj.gentex_layers (set up via "
                "Blender's UI). Returns the baked image name + base64 PNG."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "object_name": {"type": "string",
                                    "description": "Object name. If omitted, uses active object."},
                    "width": {"type": "integer", "default": 2048},
                    "height": {"type": "integer", "default": 2048},
                },
                "required": [],
            },
        ),
        Tool(
            name="list_pipelines",
            description=(
                "List the GenTexture pipeline node trees stored in this "
                ".blend (bpy.data.node_groups of type "
                "GenTexPipelineNodeTree). Use `run_pipeline` with the "
                "returned name + `keep_tree: true` to re-execute one."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="describe_pipeline_schema",
            description=(
                "Return the catalog of available pipeline node types, "
                "including each node's bl_idname, label, declared properties "
                "(with defaults / min / max / enum choices), and the names + "
                "ids of its input and output sockets. Read this first when "
                "authoring a `run_pipeline` graph so you know which sockets "
                "to link and which props to set."
            ),
            inputSchema={"type": "object", "properties": {}, "required": []},
        ),
        Tool(
            name="run_pipeline",
            description=(
                "Execute a GenTexture pipeline described as JSON. The graph "
                "is built into a `GenTexPipelineNodeTree` (transient by "
                "default, persisted if `keep_tree: true`) and run end-to-end "
                "via the same executor used by the in-Blender UI button. "
                "Returns one entry per terminal node: every Output Image "
                "result comes back as its own ImageContent so you can see "
                "them inline; Project Layer results report the object name "
                "and the layers that were added.\n\n"
                "Graph shape:\n"
                "  {\n"
                '    \"name\": \"iron_pipeline\",            # optional, reuses '
                "or creates a node group\n"
                '    \"keep_tree\": false,                  # keep group around '
                "in bpy.data after run\n"
                '    \"nodes\": [\n'
                '      {\"id\": \"p\",   \"type\": \"GenTexNodeText\",'
                '        \"props\": {\"text\": \"rusted iron\"}},\n'
                '      {\"id\": \"g\",   \"type\": \"GenTexNodeGenerate\",'
                '    \"props\": {\"provider\": \"gemini_direct\"}},\n'
                '      {\"id\": \"out\", \"type\": \"GenTexNodeOutputImage\", '
                '\"props\": {\"output_name\": \"iron_albedo\"}}\n'
                '    ],\n'
                '    \"links\": [\n'
                '      {\"from\": [\"p\", \"Text\"], \"to\": [\"g\", \"Prompt\"]},\n'
                '      {\"from\": [\"g\", \"Image\"], \"to\": [\"out\", \"Image\"]}\n'
                '    ]\n'
                "  }\n\n"
                "Call `describe_pipeline_schema` for the full list of node "
                "types, their props, and their socket names."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "graph": {
                        "type": "object",
                        "description": "Pipeline graph as documented above.",
                    },
                    "timeout": {
                        "type": "number",
                        "default": 600,
                        "description": "Max seconds to wait for completion.",
                    },
                },
                "required": ["graph"],
            },
        ),
    ]


# ---------------------------------------------------------------------------
# Image-returning tools — convention: list contents
# ---------------------------------------------------------------------------

_IMAGE_RETURN_TOOLS = {"text2img", "img2img", "inpaint", "get_image", "bake_layers"}


# ---------------------------------------------------------------------------
# TCP transport to Blender
# ---------------------------------------------------------------------------

def _send_command_sync(cmd_type: str, params: dict) -> dict:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(SOCK_TIMEOUT)
    try:
        sock.connect((BLENDER_HOST, BLENDER_PORT))
        sock.sendall(json.dumps({"type": cmd_type, "params": params}).encode("utf-8"))
        # Signal end-of-request so the addon stops waiting for more bytes.
        sock.shutdown(socket.SHUT_WR)

        buf = bytearray()
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf.extend(chunk)
        if not buf:
            raise RuntimeError("Empty response from Blender")

        try:
            envelope = json.loads(buf.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Bad JSON from Blender: {e}; head={bytes(buf[:200])!r}")

        if envelope.get("status") == "error":
            raise RuntimeError(envelope.get("message", "Unknown error from Blender"))
        return envelope.get("result", {})
    except ConnectionRefusedError:
        raise RuntimeError(
            f"Cannot reach Blender at {BLENDER_HOST}:{BLENDER_PORT}. "
            f"Is Blender running with the GenTexture addon enabled and the "
            f"MCP server toggled on in addon preferences?"
        )
    finally:
        try:
            sock.close()
        except OSError:
            pass


async def _send_command(cmd_type: str, params: dict) -> dict:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _send_command_sync, cmd_type, params)


# ---------------------------------------------------------------------------
# MCP server wiring
# ---------------------------------------------------------------------------

def _make_response(tool: str, result: dict) -> list[Any]:
    """Build MCP response content list.

    Image-returning tools include an ImageContent block so the agent sees
    the result; the matching TextContent carries the metadata. `run_pipeline`
    fans out every image-kind output as its own ImageContent.
    """
    images: list[str] = []

    if tool == "run_pipeline" and isinstance(result, dict):
        for entry in result.get("outputs") or []:
            b64 = entry.pop("image_base64", None) if isinstance(entry, dict) else None
            if b64:
                images.append(b64)
    elif isinstance(result, dict):
        b64 = result.pop("image_base64", None)
        if tool in _IMAGE_RETURN_TOOLS and b64:
            images.append(b64)

    out: list[Any] = [TextContent(type="text", text=json.dumps(result, indent=2))]
    for b64 in images:
        out.append(ImageContent(type="image", data=b64, mimeType="image/png"))
    return out


def _make_server() -> Server:
    server = Server("gentex")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return _tool_list()

    @server.call_tool()
    async def call_tool(name: str, arguments: Any) -> list[Any]:
        try:
            result = await _send_command(name, arguments or {})
            return _make_response(name, result)
        except Exception as e:
            return [TextContent(type="text", text=f"Error: {e}")]

    return server


async def main():
    server = _make_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


if __name__ == "__main__":
    asyncio.run(main())
