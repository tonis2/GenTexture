"""TCP JSON command server hosted inside Blender.

Wire protocol (one round-trip per connection):

    request:  {"type": <command>, "params": {...}}
    response: {"status": "ok", "result": {...}}
              {"status": "error", "message": "..."}

The server runs in a background thread. Each accepted connection runs on
its own short-lived worker thread. Command handlers in `commands.py`
marshal bpy access onto the main thread via `main_thread.run_on_main`.

Modeled on /run/media/tonis/extra/blender/blender-mcp/blender_mcp_addon.py
to stay consistent with the existing Blender-MCP addon pattern.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import traceback

from .commands import COMMANDS


_state: dict = {
    "thread": None,
    "socket": None,
    "running": False,
    "host": "127.0.0.1",
    "port": 9877,
    "error": "",
}


def is_running() -> bool:
    return bool(_state.get("running"))


def get_address() -> tuple[str, int]:
    return _state.get("host", "127.0.0.1"), int(_state.get("port", 9877))


def get_last_error() -> str:
    return _state.get("error", "")


def start_server(host: str = "127.0.0.1", port: int = 9877) -> None:
    """Start the listener on (host, port). Idempotent: stops first if running."""
    if _state["running"]:
        stop_server()

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((host, port))
    except OSError as e:
        sock.close()
        _state["error"] = f"bind({host}:{port}) failed: {e}"
        print(f"GenTexMCP: {_state['error']}")
        return
    sock.listen(8)
    sock.settimeout(1.0)  # so the accept loop can poll _state["running"]

    _state.update({
        "socket": sock,
        "running": True,
        "host": host,
        "port": port,
        "error": "",
    })

    t = threading.Thread(target=_accept_loop, name="GenTexMCP-listen", daemon=True)
    _state["thread"] = t
    t.start()
    print(f"GenTexMCP: listening on {host}:{port}")


def stop_server() -> None:
    if not _state["running"]:
        return
    _state["running"] = False
    sock = _state.get("socket")
    if sock is not None:
        try:
            sock.close()
        except Exception:
            pass
    _state["socket"] = None
    t = _state.get("thread")
    if t is not None and t.is_alive():
        t.join(timeout=2.0)
    _state["thread"] = None
    print("GenTexMCP: stopped")


def _accept_loop():
    sock = _state["socket"]
    while _state["running"]:
        try:
            client, addr = sock.accept()
        except socket.timeout:
            continue
        except OSError:
            # Socket was closed during stop_server.
            break
        except Exception as e:
            if _state["running"]:
                print(f"GenTexMCP: accept error: {e}")
            time.sleep(0.2)
            continue
        threading.Thread(
            target=_handle_connection, args=(client, addr),
            name=f"GenTexMCP-conn-{addr[1]}", daemon=True,
        ).start()


def _recv_all_json(client: socket.socket, max_bytes: int = 64 * 1024 * 1024) -> dict:
    """Read until the buffer parses as a complete JSON object.

    The wire format is one JSON request per connection (the client closes
    its send half after writing, or sends a complete object and waits for a
    reply). We try `json.loads` after each chunk and break when it parses.
    `max_bytes` bounds memory if the client misbehaves.
    """
    buf = bytearray()
    client.settimeout(60.0)
    while True:
        chunk = client.recv(65536)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > max_bytes:
            raise ValueError(f"request exceeds {max_bytes} bytes")
        try:
            return json.loads(buf.decode("utf-8"))
        except json.JSONDecodeError:
            continue
        except UnicodeDecodeError:
            # Partial multi-byte char at boundary — keep reading.
            continue
    raise ValueError("connection closed before a complete JSON request")


def _send_json(client: socket.socket, obj: dict) -> None:
    data = json.dumps(obj).encode("utf-8")
    client.sendall(data)


def _handle_connection(client: socket.socket, addr) -> None:
    try:
        try:
            request = _recv_all_json(client)
        except Exception as e:
            _send_json(client, {"status": "error", "message": f"bad request: {e}"})
            return

        cmd_type = request.get("type")
        params = request.get("params") or {}

        handler = COMMANDS.get(cmd_type)
        if handler is None:
            _send_json(client, {
                "status": "error",
                "message": f"unknown command '{cmd_type}'. "
                           f"Available: {sorted(COMMANDS.keys())}",
            })
            return

        try:
            result = handler(params)
            _send_json(client, {"status": "ok", "result": result})
        except Exception as e:
            tb = traceback.format_exc()
            print(f"GenTexMCP: command '{cmd_type}' failed:\n{tb}")
            _send_json(client, {
                "status": "error",
                "message": f"{type(e).__name__}: {e}",
            })
    finally:
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            client.close()
        except OSError:
            pass
