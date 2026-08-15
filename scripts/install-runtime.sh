#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
install_root="${LONGRUN_INSTALL_DIR:-$HOME/.local/share/codex-longrun-mcp}"
runtime_root="$install_root/.venv"

command -v uv >/dev/null 2>&1 || {
    echo "uv is required" >&2
    exit 1
}

install -d -m 700 "$install_root"
UV_PROJECT_ENVIRONMENT="$runtime_root" \
    uv sync --project "$repo_root" --frozen --no-dev --no-editable \
    --reinstall-package codex-mcp-longrun

uv pip check --python "$runtime_root/bin/python"
printf 'Installed runtime: %s\n' "$runtime_root"
printf 'Server command: %s\n' "$runtime_root/bin/codex-mcp-longrun"
printf 'Bridge launcher: %s\n' "$runtime_root/bin/codex-longrun"
printf 'Bridge service: %s\n' "$runtime_root/bin/codex-longrun-bridge"
