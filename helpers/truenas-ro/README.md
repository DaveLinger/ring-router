# Read-only host access for Claude Code on TrueNAS

On TrueNAS nearly every useful check (docker logs, `zpool status`, the middleware API) needs root,
and the account Claude Code normally SSHes into (`claude`) has full passwordless sudo. Claude Code's
first, look-only run gets a separate path instead:

| Piece | Where | What it does |
|---|---|---|
| `claude-ro` user | TrueNAS (created through the API, so it survives reboots) | No password, no admin or docker group; sudo for exactly `rr-ro` |
| `rr-ro` | `/mnt/SSD/rr-ro/rr-ro`, root-owned | Runs read-only root commands from a fixed list (docker ps/logs/inspect, zpool/zfs reads, `midclt call` read methods, journalctl, dmesg, smartctl, systemctl status, ss, ps, and cat/head/tail/ls/grep under `/var/log` and `/mnt/.ix-apps/app_configs`); refuses everything else |
| `truenas-ro` | container: `/home/developer/.claude/bin/truenas-ro` | `ssh` to the host as `claude-ro` with its own key; arguments are the remote command only, so a run can't switch user or key |
| `extra_read_tools`, `read_hint` | `[hosts.truenas]` in `config.toml` | Allow `Bash(/home/developer/.claude/bin/truenas-ro:*)` in the read run and tell Claude Code how to use it |

The Proceed run is unchanged: `ssh truenas` as `claude`, with sudo.

## Install

```sh
# container: a key for claude-ro (persistent ~/.ssh mount)
sudo docker exec claude ssh-keygen -q -t ed25519 -N "" -C claude-ro@container -f /home/developer/.ssh/truenas_ro_ed25519
# host: the wrapper and the user
sudo bash install.sh "$(sudo docker exec claude cat /home/developer/.ssh/truenas_ro_ed25519.pub)" /mnt/SSD
# container: the launcher
sudo docker cp truenas-ro claude:/home/developer/.claude/bin/truenas-ro
```

Check: `sudo docker exec claude /home/developer/.claude/bin/truenas-ro sudo -n /mnt/SSD/rr-ro/rr-ro zpool list`
works, and `... rr-ro zfs destroy x` / `... sudo -n true` are refused.
