#!/bin/bash
# Install the read-only account for Claude Code's look-only runs on TrueNAS SCALE. Run as root:
#
#   sudo bash install.sh "ssh-ed25519 AAAA... claude-ro@container" [/mnt/SSD]
#
# Creates (through the TrueNAS API, so it survives reboots and upgrades):
#   <pool>/rr-ro/rr-ro      the wrapper, root-owned (the system disk is read-only on SCALE)
#   user claude-ro          no password, the given SSH key, no admin groups, and sudo for exactly
#                           the wrapper with no password
set -euo pipefail
pub=${1:?usage: install.sh "<ssh public key>" [pool path]}; pool=${2:-/mnt/SSD}
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)
dir=$pool/rr-ro; wrapper=$dir/rr-ro
install -d -o root -g root -m 755 "$dir"
install -o root -g root -m 755 "$here/rr-ro" "$wrapper"

existing=$(midclt call user.query '[["username","=","claude-ro"]]' '{"select":["id"]}')
python3 - "$pub" "$wrapper" "$pool" "$existing" <<'PY'
import json, subprocess, sys
pub, wrapper, pool, existing = sys.argv[1:]
fields = {"full_name": "Claude Code read-only (ring-router)", "shell": "/usr/bin/bash",
          "password_disabled": True, "sshpubkey": pub, "smb": False,
          "sudo_commands": [], "sudo_commands_nopasswd": [wrapper]}
found = json.loads(existing)
if found:
    subprocess.run(["midclt", "call", "user.update", str(found[0]["id"]), json.dumps(fields)], check=True,
                   stdout=subprocess.DEVNULL)
    print("updated user claude-ro")
else:
    fields |= {"username": "claude-ro", "group_create": True, "home": pool, "home_create": True}   # TrueNAS creates <pool>/claude-ro
    subprocess.run(["midclt", "call", "user.create", json.dumps(fields)], check=True, stdout=subprocess.DEVNULL)
    print("created user claude-ro")
PY
id claude-ro
sudo -l -U claude-ro | tail -3
