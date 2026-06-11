#!/bin/bash
set -e

# Fallback to 8080 if PORT is not injected
PORT=${PORT:-8080}

echo "Starting gunicorn on port $PORT"
exec gunicorn app:app --workers 2 --threads 2 --bind "0.0.0.0:$PORT"
