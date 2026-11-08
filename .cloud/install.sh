#!/usr/bin/env bash
set -euo pipefail
cd /workspace/Acortador-de-URLs
export UV_CACHE_DIR="${UV_CACHE_DIR:-/workspace/.cache/uv}"
export UV_LINK_MODE=copy
uv sync --frozen
docker compose -f compose.yaml -f compose.cloud.yaml build
