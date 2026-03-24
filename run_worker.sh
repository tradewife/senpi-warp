#!/bin/bash
set -a
source .env
set +a
exec venv/bin/python3 worker.py "$@"