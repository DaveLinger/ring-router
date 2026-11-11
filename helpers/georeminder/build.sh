#!/bin/zsh
# Build the georeminder helper into <repo>/build/georeminder
set -euo pipefail
here=${0:A:h}; out="${here:h:h}/build"; mkdir -p "$out"
swiftc -O -o "$out/georeminder" "$here/georeminder.swift" -framework EventKit -framework CoreLocation \
  -Xlinker -sectcreate -Xlinker __TEXT -Xlinker __info_plist -Xlinker "$here/Info.plist"
codesign --force --sign - --identifier local.ring-router.georeminder "$out/georeminder"
echo "Built: $out/georeminder"
