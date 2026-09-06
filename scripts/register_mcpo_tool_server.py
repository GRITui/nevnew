#!/usr/bin/env python3
"""
Register mcpo bridge as an OpenAPI tool server in Open-WebUI's database.

This script is idempotent and can be run on startup to ensure the tool server
is configured. It does NOT enable the tool server for all chats automatically —
Open-WebUI v0.11.3 has no API for global default tool-enablement. Users must
still enable the tool server per chat in the UI (Tools → select server).

Usage:
    python3 scripts/register_mcpo_tool_server.py
"""

import json
import os
import sqlite3
import sys

DB_PATH = "/app/backend/data/webui.db"
# In container: /app/backend/data/webui.db
# On host: ./open-webui/data/webui.db (adjust as needed)

MCPO_API_KEY = os.environ.get("MCPO_API_KEY")
if not MCPO_API_KEY:
    print("ERROR: MCPO_API_KEY is not set (source .env first).")
    sys.exit(1)

TOOL_SERVER_CONFIG = {
    "type": "openapi",
    "url": "http://mcpo:8000",
    "key": MCPO_API_KEY,
    "auth_type": "bearer",
    "path": "openapi.json",
    "spec_type": "url",
    "config": {"enable": True},
    "info": {
        "id": "mcpo-n8n-mcp",
        "name": "NevNew MCP via mcpo",
        "description": "MCP Server Trigger via mcpo bridge"
    }
}

def get_db_path():
    """Find the Open-WebUI database file."""
    paths = [
        "/app/backend/data/webui.db",  # Inside container
        "./open-webui/data/webui.db",  # Host mount (if using named volume, this won't work)
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    # Try to find via docker volume
    import subprocess
    try:
        result = subprocess.run(
            ["docker", "volume", "inspect", "newnew_open_webui_data"],
            capture_output=True, text=True, check=True
        )
        import json as json_mod
        vol_info = json_mod.loads(result.stdout)
        mountpoint = vol_info[0]["Mountpoint"]
        db_path = os.path.join(mountpoint, "webui.db")
        if os.path.exists(db_path):
            return db_path
    except Exception:
        pass
    return None

def main():
    db_path = get_db_path()
    if not db_path:
        print("ERROR: Could not find Open-WebUI database. Tried:")
        print("  - /app/backend/data/webui.db")
        print("  - ./open-webui/data/webui.db")
        print("  - Docker volume 'newnew_open_webui_data'")
        sys.exit(1)

    print(f"Using database: {db_path}")

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Read current tool_server.connections
    cursor.execute('SELECT value FROM config WHERE key = "tool_server.connections";')
    row = cursor.fetchone()
    current = json.loads(row[0]) if row and row[0] else []

    # Check if already registered
    already_registered = any(
        s.get("info", {}).get("id") == "mcpo-n8n-mcp"
        for s in current
    )

    if already_registered:
        print("Tool server 'mcpo-n8n-mcp' already registered.")
    else:
        current.append(TOOL_SERVER_CONFIG)
        new_value = json.dumps(current)
        cursor.execute(
            'UPDATE config SET value = ? WHERE key = "tool_server.connections";',
            (new_value,)
        )
        conn.commit()
        print("Registered tool server 'mcpo-n8n-mcp' in tool_server.connections")

    # Also create/update model entry with default toolIds for automations/channels
    # This doesn't affect regular chat but ensures automations can use the tools.
    cursor.execute('SELECT id FROM model WHERE id = "NevNew";')
    model_row = cursor.fetchone()

    model_meta = {
        "toolIds": ["server:mcpo-n8n-mcp"],
        "capabilities": {
            "function_calling": True,
            "tools": True
        }
    }

    now = int(__import__('time').time())
    if model_row:
        cursor.execute(
            'UPDATE model SET meta = ?, updated_at = ? WHERE id = "NevNew";',
            (json.dumps(model_meta), now)
        )
        print("Updated model 'NevNew' with default toolIds")
    else:
        cursor.execute(
            '''INSERT INTO model (id, user_id, base_model_id, name, meta, params, is_active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (
                "NevNew",
                "97705eda-da41-4bf9-b3f0-4fa87bf4beae",  # admin user
                None,
                "NevNew",
                json.dumps(model_meta),
                json.dumps({}),
                1,
                now,
                now
            )
        )
        print("Created model 'NevNew' entry with default toolIds")

    conn.commit()
    conn.close()
    print("\nDone. Restart Open-WebUI to pick up changes:")
    print("  docker restart newnew-open-webui")
    print("\nNOTE: Tools still need to be enabled per chat in the UI.")
    print("      Open-WebUI v0.11.3 has no global default tool-enablement API.")

if __name__ == "__main__":
    main()