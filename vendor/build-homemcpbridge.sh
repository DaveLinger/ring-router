#!/bin/zsh
# Fetch HomeMCPBridge, apply ring-router's patch, and build it signed with your Apple ID team.
#   vendor/build-homemcpbridge.sh <TEAM_ID>        (Xcode → Settings → Accounts; a free Personal Team works)
# The patch adds HOMEMCP_HOMES (limit to one HomeKit home), control by exact device id, Fan v2, HomeKit's
# lock-mechanism characteristics (upstream used wrong IDs), and a
# unique bundle id. Built app: vendor/HomeMCPBridge/build/Build/Products/Release-maccatalyst/
set -euo pipefail
team=${1:?usage: $0 <TEAM_ID>}
here=${0:A:h}; cd "$here"
rev=01e3674   # upstream commit the patch was made against
[[ -d HomeMCPBridge ]] || git clone -q https://github.com/coalsi/HomeMCPBridge.git
cd HomeMCPBridge && git checkout -q "$rev" -- . && git apply ../patches/HomeMCPBridge-ring-router.patch
sed -i '' "s/DEVELOPMENT_TEAM = [^;]*;/DEVELOPMENT_TEAM = $team;/g" HomeMCPBridge.xcodeproj/project.pbxproj
DEVELOPER_DIR=${DEVELOPER_DIR:-/Applications/Xcode.app/Contents/Developer} xcodebuild \
  -project HomeMCPBridge.xcodeproj -scheme HomeMCPBridge -configuration Release \
  -destination 'platform=macOS,variant=Mac Catalyst' -derivedDataPath build -allowProvisioningUpdates build \
  | grep -E "error:|BUILD (SUCCEEDED|FAILED)"
