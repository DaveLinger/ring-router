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
# Sign with an Apple Development certificate when one exists: macOS then tracks the app by
# developer identity, so privacy grants survive rebuilds. Falls back to ad-hoc (grants reset on rebuild).
ident=$(security find-identity -v -p codesigning | awk -F'"' '/Apple Development/{print $2; exit}')
codesign --force --sign "${ident:--}" --identifier local.ring-router --options runtime "$app"
echo "Signed with: ${ident:-ad-hoc}"
echo "Built: $app"
