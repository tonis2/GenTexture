"""Start/Stop operators for the MCP server, exposed in addon preferences."""

import bpy

from . import server


def _prefs():
    pkg = __package__.rsplit(".", 1)[0]
    return bpy.context.preferences.addons[pkg].preferences


class GENTEX_OT_McpStart(bpy.types.Operator):
    bl_idname = "gentex.mcp_start"
    bl_label = "Start MCP Server"
    bl_description = "Open the TCP socket so external AI agents can connect"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        prefs = _prefs()
        server.start_server(prefs.mcp_host, int(prefs.mcp_port))
        if server.is_running():
            self.report({'INFO'}, f"MCP server listening on {prefs.mcp_host}:{prefs.mcp_port}")
        else:
            self.report({'ERROR'}, server.get_last_error() or "Failed to start MCP server")
        return {'FINISHED'}


class GENTEX_OT_McpStop(bpy.types.Operator):
    bl_idname = "gentex.mcp_stop"
    bl_label = "Stop MCP Server"
    bl_description = "Close the MCP server socket"
    bl_options = {'REGISTER', 'INTERNAL'}

    def execute(self, context):
        server.stop_server()
        self.report({'INFO'}, "MCP server stopped")
        return {'FINISHED'}


classes = (GENTEX_OT_McpStart, GENTEX_OT_McpStop)
