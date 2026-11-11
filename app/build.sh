#!/bin/zsh
# Build "Ring Router.app" (the launcher that gives the listener a stable macOS app identity).
# Usage: app/build.sh [command...]   (default command: <repo>/.venv/bin/ring-router serve)
set -euo pipefail
here=${0:A:h}; repo=${here:h}
app="$repo/build/Ring Router.app"
rm -rf "$app"; mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources"
cp "$here/Info.plist" "$app/Contents/Info.plist"
swiftc -O -o "$app/Contents/MacOS/RingRouter" "$here/launcher.swift"
if (( $# )); then printf '%s\n' "$@" > "$app/Contents/Resources/command.txt"
else printf '%s\n' "$repo/.venv/bin/ring-router" "serve" > "$app/Contents/Resources/command.txt"; fi
codesign --force --sign - --identifier local.ring-router "$app"
echo "Built: $app"
