#!/usr/bin/env python3
import json
import os
import urllib.request
from pathlib import Path

# Load .env
for line in Path('.env').read_text().splitlines():
    line = line.strip()
    if not line or line.startswith('#'): continue
    if '=' in line:
        k, _, v = line.partition('=')
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v

token = os.environ.get('SENPI_AUTH_TOKEN', '')
print(f"Token: {token[:50]}..." if token else "No token")
print(f"Token length: {len(token)}")

# Try discovery_get_top_traders
url = os.environ.get("SENPI_MCP_URL", "https://mcp.prod.senpi.ai/mcp")
payload = json.dumps({
    "jsonrpc": "2.0",
    "id": 1,
    "method": "tools/call",
    "params": {"name": "discovery_get_top_traders", "arguments": {"time_frame": "MONTHLY", "limit": 5}},
}).encode()

req = urllib.request.Request(
    url,
    data=payload,
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
)
try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())
        print("Response:", json.dumps(body, indent=2)[:1000])
except Exception as e:
    print(f"Error: {e}")