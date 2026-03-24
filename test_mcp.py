#!/usr/bin/env python3
import os
import sys
import json
from pathlib import Path

# Load .env file
env_path = Path('.env')
if env_path.exists():
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                try:
                    key, value = line.split('=', 1)
                    os.environ[key] = value
                except ValueError:
                    pass

# Add lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts" / "lib"))

from senpi_common import mcporter_call

print("Testing Senpi MCP connectivity...")
result = mcporter_call("account_get_portfolio", {}, timeout=15)
print("Result:", json.dumps(result, indent=2))

if "error" in result:
    print(f"ERROR: {result['error']}")
    sys.exit(1)
else:
    print("SUCCESS: MCP connection works")
    sys.exit(0)