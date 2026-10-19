#!/bin/sh
# lights-out installer: puts the `lightsout` command on your PATH and writes a starter config.
#   curl -fsSL https://raw.githubusercontent.com/PepSecanell/lights-out/main/install.sh | sh
# It uses uv if you have it, then pipx, then pip --user. Nothing runs as root.
set -eu
SRC="${LIGHTSOUT_SRC:-git+https://github.com/PepSecanell/lights-out}"

if command -v uv >/dev/null 2>&1; then
  uv tool install --force "$SRC"
elif command -v pipx >/dev/null 2>&1; then
  pipx install --force "$SRC"
else
  python3 -m pip install --user --upgrade "$SRC"
fi

if ! command -v lightsout >/dev/null 2>&1; then
  echo "Installed, but 'lightsout' is not on your PATH yet. Open a new terminal, or add ~/.local/bin to PATH."
  exit 0
fi
command -v claude >/dev/null 2>&1 || echo "Note: 'claude' (Claude Code) was not found. Install it first: https://docs.claude.com/en/docs/claude-code"
lightsout init || true
