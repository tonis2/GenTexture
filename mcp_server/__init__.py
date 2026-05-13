"""GenTexture MCP bridge — TCP JSON command dispatcher hosted inside Blender.

The addon exposes a pure-stdlib socket server here. A separate stdio MCP
server process (`mcp/gentex-mcp-server.py`) is what an AI agent client
(Claude Code, OpenCode, etc.) actually launches; it translates MCP tool
calls into commands on this socket.

This split keeps the addon free of the `mcp` Python SDK dependency, which
Blender's bundled Python can't easily install.
"""

from .server import start_server, stop_server, is_running, get_address, get_last_error

__all__ = ["start_server", "stop_server", "is_running", "get_address", "get_last_error"]
