#!/bin/sh
# Install ring-router's "sudo only during Proceed" switch on this host. Run as root:
#
#   Linux VPS:  sudo sh install.sh linux dave "ssh-ed25519 AAAA... ring-router-sudo"
#   Mac mini:   sudo sh install.sh mac dave
#
# linux: a locked-down account, rr-sudo, whose only SSH key (kept on the Mac mini) can run nothing
#        but the switch. dave can't flip it, so dave still needs a password for sudo otherwise.
# mac:   ring-router runs as dave here, so dave may run the switch without a password (sudoers rule
#        for exactly that command). That limits mistakes and time, not someone already in dave's
#        account: they could flip it too.
# Both:  a timer runs "sweep" every 5 minutes, so a grant never outlives 30 minutes.
set -eu
mode=${1:?usage: install.sh linux|mac TARGET_USER [PUBKEY]}; target=${2:?target user}; pub=${3:-}
[ "$(id -u)" = 0 ] || { echo "run as root (sudo)" >&2; exit 1; }
id "$target" >/dev/null
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p /usr/local/sbin
sed "s/@TARGET@/$target/" "$here/ring-router-sudo" > /usr/local/sbin/ring-router-sudo
chown root /usr/local/sbin/ring-router-sudo; chmod 755 /usr/local/sbin/ring-router-sudo

rule() {   # $1 = sudoers.d file, $2 = rule line
  tmp=$(mktemp); printf '%s\n' "$2" > "$tmp"
  visudo -cqf "$tmp" || { rm -f "$tmp"; echo "bad sudoers rule" >&2; exit 1; }
  chmod 440 "$tmp"; chown root "$tmp"; mv "$tmp" "$1"
}

case $mode in
linux)
  [ -n "$pub" ] || { echo "linux needs the ring-router-sudo public key" >&2; exit 1; }
  if ! id rr-sudo >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /var/lib/rr-sudo --shell /bin/sh rr-sudo
  fi
  usermod -p '*' rr-sudo          # no password, but not "locked" (sshd refuses locked accounts)
  cat > /usr/local/sbin/ring-router-sudo-ssh <<'W'
#!/bin/sh
# Forced command for rr-sudo's key: pass "on <id>" / "off <id>" / "status" to the switch, nothing else.
set -- ${SSH_ORIGINAL_COMMAND:-}
exec sudo -n /usr/local/sbin/ring-router-sudo "${1:-}" "${2:-}"
W
  chmod 755 /usr/local/sbin/ring-router-sudo-ssh
  install -d -m 700 -o rr-sudo -g rr-sudo /var/lib/rr-sudo/.ssh
  printf 'restrict,command="/usr/local/sbin/ring-router-sudo-ssh" %s\n' "$pub" > /var/lib/rr-sudo/.ssh/authorized_keys
  chown rr-sudo:rr-sudo /var/lib/rr-sudo/.ssh/authorized_keys; chmod 600 /var/lib/rr-sudo/.ssh/authorized_keys
  rule /etc/sudoers.d/ring-router-sudo-switch "rr-sudo ALL=(root) NOPASSWD: /usr/local/sbin/ring-router-sudo"
  cat > /etc/systemd/system/ring-router-sudo-sweep.service <<'U'
[Unit]
Description=Expire ring-router Proceed sudo grants
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/ring-router-sudo sweep
U
  cat > /etc/systemd/system/ring-router-sudo-sweep.timer <<'U'
[Unit]
Description=Expire ring-router Proceed sudo grants every 5 minutes
[Timer]
OnBootSec=1min
OnUnitActiveSec=5min
[Install]
WantedBy=timers.target
U
  systemctl daemon-reload
  systemctl enable --now ring-router-sudo-sweep.timer
  if sshd -T 2>/dev/null | grep -qiE '^(allowusers|allowgroups) '; then
    echo "NOTE: sshd has AllowUsers/AllowGroups; add rr-sudo there or its key can't log in." >&2
  fi
  ;;
mac)
  rule /etc/sudoers.d/ring-router-sudo-switch "$target ALL=(root) NOPASSWD: /usr/local/sbin/ring-router-sudo"
  cat > /Library/LaunchDaemons/local.ring-router-sudo-sweep.plist <<'P'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>local.ring-router-sudo-sweep</string>
  <key>ProgramArguments</key><array><string>/usr/local/sbin/ring-router-sudo</string><string>sweep</string></array>
  <key>StartInterval</key><integer>300</integer>
  <key>RunAtLoad</key><true/>
</dict></plist>
P
  chown root:wheel /Library/LaunchDaemons/local.ring-router-sudo-sweep.plist
  chmod 644 /Library/LaunchDaemons/local.ring-router-sudo-sweep.plist
  launchctl bootout system/local.ring-router-sudo-sweep 2>/dev/null || true
  launchctl bootstrap system /Library/LaunchDaemons/local.ring-router-sudo-sweep.plist
  ;;
*) echo "mode must be linux or mac" >&2; exit 2 ;;
esac
/usr/local/sbin/ring-router-sudo sweep >/dev/null
echo "installed: ring-router-sudo ($mode) for $target; status: $(/usr/local/sbin/ring-router-sudo status)"
