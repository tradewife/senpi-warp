#!/usr/bin/env python3
import os
import sys
from pathlib import Path

# Load .env file using python-dotenv if available, otherwise manual
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # Manual fallback
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

# Now import and run the worker
sys.path.insert(0, str(Path.cwd()))
from worker import main

if __name__ == '__main__':
    main()