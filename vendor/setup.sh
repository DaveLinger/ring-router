#!/bin/zsh
# Install the pinned mcp-macos, apply ring-router's patches, and build its EventKit helper.
# (npm blocks mcp-macos's own postinstall build, so it's done here explicitly.)
set -euo pipefail
here=${0:A:h}; cd "$here"
npm install --no-audit --no-fund
for p in patches/*.patch; do
  patch -p1 -N -d node_modules/mcp-macos < "$p" || echo "($p already applied)"
done
(cd node_modules/mcp-macos && node scripts/build-swift.mjs)
