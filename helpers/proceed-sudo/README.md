# Sudo only during Proceed

Claude Code's first run on a host only looks. A Proceed tap (or "go ahead") resumes the session
with change tools, but `sudo` still wants a password nobody can type. This switch gives the host
user **passwordless sudo only while a Proceed run is going**:

1. ring-router calls `ring-router-sudo on <run id>` just before the Proceed run,
2. Claude Code is told it may use `sudo -n`,
3. ring-router calls `ring-router-sudo off <run id>` when the run ends, however it ends,
4. a timer runs `ring-router-sudo sweep` every 5 minutes and drops any grant older than 30 minutes.

The sudoers rule (`/etc/sudoers.d/zz-ring-router-proceed`) exists only while a grant is live, and
every on/off is logged (`ring-router-sudo` tag in syslog / `log show`).

## Linux host (VPS)

The switch is reached through its own account, `rr-sudo`, whose only SSH key lives on the Mac mini
(`~/.ssh/ring-router-sudo_ed25519`) and is pinned to the switch with a forced command. The host
user can't flip it, so they still need a password for sudo the rest of the time. Root login stays off.

```sh
scp -r helpers/proceed-sudo dave@host:~/
ssh -t dave@host 'sudo sh ~/proceed-sudo/install.sh linux dave "$(cat ~/.ssh/ring-router-sudo_ed25519.pub)"'
```

(Run the second line from the Mac mini so `$(cat …)` reads the right key, or paste the key in.)
Then set `proceed_sudo = "ssh:rr-sudo@host"` for that host in `config.toml`.

## This Mac

ring-router runs as you here, so you may run the switch without a password (a sudoers rule for
exactly that command). That limits mistakes and time, not someone already in your account.

```sh
sudo sh helpers/proceed-sudo/install.sh mac dave
```

Then set `proceed_sudo = "local"` for the Mac's host entry.

## Check

```sh
ssh -i ~/.ssh/ring-router-sudo_ed25519 rr-sudo@host status     # VPS: prints off
sudo -n /usr/local/sbin/ring-router-sudo status                 # Mac: prints off
```
