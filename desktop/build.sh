#!/usr/bin/env bash
# Build the Cacophony desktop application (design document section 41).
#
#   ./desktop/build.sh            a debug shell, using `cacophony` from PATH
#   ./desktop/build.sh --bundle   an installer, with the backend inside it
#
# The shell is the easy half. The hard half is the *runtime*: a user who
# double-clicks an icon must not need Python, so a release bundles an
# interpreter with the backend frozen into it.
#
# What this script can do depends on where it runs. Cross-compiling a macOS or
# Windows binary from Linux is not possible for either half - Tauri needs the
# platform's own webview, and PyInstaller needs the platform's own interpreter -
# so a release is built once per platform, which is what the CI workflow beside
# this file does.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE=0
[[ "${1:-}" == "--bundle" ]] && BUNDLE=1

say() { printf '\033[35m▸\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. The Studio. Tauri serves the same build `cacophony serve` serves; section
#    41 requires that web deployment remain possible, and one build is the
#    cheapest way to be sure they cannot drift.
# ---------------------------------------------------------------------------
say "Building the Studio"
npm --prefix "$REPO/frontend" install --silent
npm --prefix "$REPO/frontend" run build

# ---------------------------------------------------------------------------
# 2. The backend, frozen. Only for a bundle: a developer build finds `cacophony`
#    on PATH, which is faster and is what you want while working on the shell.
# ---------------------------------------------------------------------------
if [[ $BUNDLE == 1 ]]; then
  say "Freezing the backend"
  python -m pip install --quiet pyinstaller

  # `cacophony-backend` is the name main.rs looks for beside the executable.
  # --collect-data ships the recipe catalogue and the built Studio, both of
  # which are read from the package directory at run time and would otherwise
  # be missing from the frozen tree.
  python -m PyInstaller \
    --name cacophony-backend \
    --onefile \
    --noconfirm \
    --clean \
    --distpath "$REPO/desktop/src-tauri/binaries" \
    --workpath "$REPO/desktop/.build" \
    --specpath "$REPO/desktop/.build" \
    --collect-data cacophony \
    --collect-submodules cacophony \
    --hidden-import uvicorn.lifespan.on \
    --hidden-import uvicorn.protocols.http.auto \
    --hidden-import uvicorn.protocols.websockets.auto \
    "$REPO/backend/cacophony/__main__.py"

  say "Frozen: $(du -h "$REPO/desktop/src-tauri/binaries/cacophony-backend" | cut -f1)"
fi

# ---------------------------------------------------------------------------
# 3. The shell.
# ---------------------------------------------------------------------------
cd "$REPO/desktop/src-tauri"
if [[ $BUNDLE == 1 ]]; then
  say "Bundling"
  cargo tauri build 2>/dev/null || cargo build --release
else
  say "Building the shell"
  cargo build
  say "Run it with:"
  echo "    CACOPHONY_BACKEND=$REPO/.venv/bin/cacophony \\"
  echo "        $REPO/desktop/src-tauri/target/debug/cacophony-desktop"
fi
