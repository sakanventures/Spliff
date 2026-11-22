"""Local MCP JSON-RPC stdio adapter."""
import json
import sqlite3
import sys

from .catalog import stats, tracks
from .review import reviews

TOOLS = [
    ("library_summary", "Count indexed tracks by quality and preservation status.", {}, stats),
    ("search_library", "Search local metadata and relative paths. Use the next cursor for more.",
     {"query": {"type": "string", "maxLength": 300},
      "scope": {"enum": ["all", "lossy", "lossless", "protected", "errors", "missing"]},
      "limit": {"type": "integer", "minimum": 1, "maximum": 100},
      "after": {"type": "integer", "minimum": 0}}, tracks),
    ("list_staged_reviews", "Show up to 100 manually staged comparisons.",
     {}, lambda db: {"items": reviews(db)}),
]
VERSIONS = {"2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25"}

def serve(db):
    initialized = False
    for line in sys.stdin:
        request_id = None
        try:
            request = json.loads(line)
        except ValueError:
            respond(None, error=(-32700, "Parse error"))
            continue
        if (not isinstance(request, dict) or request.get("jsonrpc") != "2.0"
                or not isinstance(request.get("method"), str)):
            respond(None, error=(-32600, "Invalid request"))
            continue
        if "id" not in request:
            continue
        request_id = request["id"]
        method = request["method"]
        params = request.get("params", {})
        if not isinstance(params, dict):
            respond(request_id, error=(-32602, "Invalid params"))
            continue
        if method == "initialize":
            version = params.get("protocolVersion")
            if not isinstance(version, str):
                respond(request_id, error=(-32602, "Protocol version required"))
                continue
            initialized = True
            respond(request_id, {
                "protocolVersion": version if version in VERSIONS else "2025-11-25",
                "serverInfo": {"name": "spliff", "title": "Spliff", "version": "0.1.0"},
                "capabilities": {"tools": {}},
            })
        elif method == "ping":
            respond(request_id, {})
        elif not initialized:
            respond(request_id, error=(-32002, "Initialize the server first"))
        elif method == "tools/list":
            respond(request_id, {"tools": [
                {"name": name, "description": description,
                 "inputSchema": {"type": "object", "properties": properties,
                                 "additionalProperties": False},
                 "annotations": {"readOnlyHint": True, "openWorldHint": False}}
                for name, description, properties, _ in TOOLS
            ]})
        elif method == "tools/call":
            arguments = params.get("arguments", {})
            tool = next((tool for tool in TOOLS if tool[0] == params.get("name")), None)
            if not tool or not isinstance(arguments, dict) or arguments.keys() - tool[2].keys():
                respond(request_id, error=(-32602, "Unknown tool or invalid arguments"))
                continue
            try:
                payload = tool[3](db, **arguments)
                result = {"content": [{"type": "text", "text": json.dumps(payload)}]}
            except (ValueError, sqlite3.Error) as exc:
                message = str(exc) if isinstance(exc, ValueError) else "Catalog unavailable"
                result = {"isError": True, "content": [{"type": "text", "text": message}]}
            respond(request_id, result)
        else:
            respond(request_id, error=(-32601, "Method not found"))

def respond(request_id, result=None, error=None):
    packet = {"jsonrpc": "2.0", "id": request_id}
    if error:
        packet["error"] = {"code": error[0], "message": error[1]}
    else:
        packet["result"] = result
    print(json.dumps(packet), flush=True)
