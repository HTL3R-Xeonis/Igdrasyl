# Deployment: gateway + per-profile backends

One gateway process owns login and the shared user store and proxies every request to the backend of the host the
caller picked; every host profile has its own backend process (`xeonis-backend@<profile>`). The units here run as
systemd *user* units of one unprivileged account. A single process started directly (`uvicorn app.main:app`) is still a
complete app without any of this.

Without `loginctl enable-linger <account>` the units only live as long as that account has a login session (see the
Debian 13 note below).

## The one thing to get exactly right

Every unit below - the gateway and every `xeonis-backend@<profile>`
instance - must set the **exact same absolute paths** for:

- `TOPOLOGYOPERATOR_USERS_FILE`
- `TOPOLOGYOPERATOR_SESSION_SECRET_FILE`
- `TOPOLOGYOPERATOR_AI_CONFIG_FILE`
- `TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR`
- `TOPOLOGYOPERATOR_BACKEND_PORTS_FILE` (gateway only - no backend ever
  reads this itself, but keep it in the same state directory as the rest)

Today's bare relative-path defaults (`users.json`, `session_secret.key`, ...)
resolve against whatever `WorkingDirectory=` happens to be. The gateway
creates the initial account in the shared store. Each managed backend needs
that same store to authenticate gateway sessions.

This is enforced: the gateway, and every backend started by `deploy/xeonis-backend-launch.py`
(`TOPOLOGYOPERATOR_GATEWAY_BACKEND=1`), refuse to start unless `TOPOLOGYOPERATOR_USERS_FILE`,
`TOPOLOGYOPERATOR_SESSION_SECRET_FILE`, `TOPOLOGYOPERATOR_AI_CONFIG_FILE` and
`TOPOLOGYOPERATOR_SETTINGS_PROFILES_DIR` are all set to absolute paths. The log and the error name the offender:
`<VAR> is not set to an absolute path - every gateway and backend unit must share the same file (see deploy/README.md).`
A standalone `uvicorn app.main:app` is not checked and keeps its relative defaults (see `app/shared_state.py`).

The two `.service` files here already set all of these identically - if
you copy them as-is and just change the `/opt/ginnungagap/...` prefix
consistently in both files, you get this right automatically. The actual
risk is editing one file's paths later and forgetting the other.

## One-time setup

1. **A dedicated, unprivileged service account** - never the account you
   deploy *from*, never root. Every unit (gateway and every backend) runs
   as this one account:
   ```bash
   sudo useradd --system --create-home --shell /usr/sbin/nologin xeonis
   sudo loginctl enable-linger xeonis
   ```
   (On Debian 13 `loginctl enable-linger` fails with "Read-only file
   system" because `systemd-logind` may not write `/var/lib/systemd/linger`;
   create the file directly instead:
   `sudo mkdir -p /var/lib/systemd/linger && sudo touch /var/lib/systemd/linger/xeonis`,
   then `sudo systemctl restart systemd-logind` or reboot.)
   `enable-linger` is required - without it, `--user` units stop the
   moment nobody is logged in as `xeonis`, which on a headless box is
   always.

2. **The shared state directory**, owned by that account:
   ```bash
   sudo install -d -m 750 -o xeonis -g xeonis /opt/ginnungagap/state \
     /opt/ginnungagap/state/settings_profiles /opt/ginnungagap/state/logs
   ```
   (adjust the path if you don't use `/opt/ginnungagap` - just change it
   consistently in both `.service` files, see above).

3. **The secrets file** both units load via `EnvironmentFile=`
   (`/opt/ginnungagap/state/xeonis.env` in the unit files here) - same
   shape as this repo's own `.env.example`, plus the AI-snippet service
   token mentioned in `TopologyBuilder/handoff/HANDOFF.md`'s AI-config
   rollout entries:
   ```
   ESXI_PASSWORD=...
   GNS3_PASSWORD=...
   XEONIS_AI_SNIPPETS_TOKEN=...
   ```
   `chmod 600` it, owned by the `xeonis` account.

4. **Install the units** (as the `xeonis` account, or copy then `chown`):
   ```bash
   mkdir -p ~xeonis/.config/systemd/user
   cp deploy/xeonis-gateway.service deploy/xeonis-backend@.service ~xeonis/.config/systemd/user/
   sudo -u xeonis XDG_RUNTIME_DIR=/run/user/$(id -u xeonis) systemctl --user daemon-reload
   sudo -u xeonis XDG_RUNTIME_DIR=/run/user/$(id -u xeonis) systemctl --user enable --now xeonis-gateway.service
   ```
   Don't `enable --now` any `xeonis-backend@` instance yet - that's what
   `app/backend_manager.py` does for you the first time someone activates
   that profile (`systemctl --user start xeonis-backend@<profile-stem>`).

5. **At least one settings profile** already in
   `/opt/ginnungagap/state/settings_profiles/` before anyone can activate
   anything - today's admin-only "upload a profile" flow
   (`POST /settings-profiles`, now on the gateway, see
   `app/routes_gateway.py`) is how you add more later.

## Why a wrapper script (`deploy/xeonis-backend-launch.py`), not a bare `ExecStart=uvicorn ...`

A plain `.service` file can't read JSON to pick its own `--port` - and the
port is only known *after* the gateway's `backend_ports.assign_port()` has
run (first activation of that profile), not at unit-install time. The
wrapper resolves the profile stem (`%i`) to its filename and port, sets
`TOPOLOGYOPERATOR_SETTINGS` and `TOPOLOGYOPERATOR_GATEWAY_BACKEND=1`, then
`exec`s uvicorn - no extra process stays
around once it has (`os.execv`, same reasoning as this project's own
`settings_profiles.restart_into_profile`).

## Per-profile log files

There's no env var for a backend's own log path - only a settings.yml's
own `log_file_path` key (see `app/logging_setup.py`). Every profile
dropped into `settings_profiles/` needs that key set to its own path
(e.g. `log_file_path: /opt/ginnungagap/state/logs/<profile-stem>.log`) -
otherwise every backend defaults to the same relative `./logs/app.log`
under the shared `WorkingDirectory=` and they overwrite each other's log.

## Verifying it

```bash
sudo -u xeonis XDG_RUNTIME_DIR=/run/user/$(id -u xeonis) systemctl --user status xeonis-gateway.service
curl http://127.0.0.1/gateway/health   # the gateway process's own liveness, not any backend's
```
Then open the gateway in a browser, log in, pick a profile on
`/pick-backend`, confirm its backend unit actually started:
```bash
sudo -u xeonis XDG_RUNTIME_DIR=/run/user/$(id -u xeonis) systemctl --user status xeonis-backend@<profile-stem>.service
```

For a parallel rollout, verify that edits in one profile do not affect the
other. Use separate backend working directories for relative state such as
design drafts. Configure any intended shared project data with absolute
paths and include it in the migration backup.

## Prepare a GNS3 VM before selecting a host

Administrators can use **Deploy GNS3 VM…** on the gateway's host picker.
Select the GNS3 OVA in the host settings first. The gateway starts the
profile's backend if needed and checks its startup result. The existing
startup logic imports the OVA only when the VM is missing. The action
keeps the caller's host selection and leaves the backend running. For an
already-running backend, it reports the current VM state. If configuration
was corrected after a failed startup, restart that backend to retry startup.

## What this does *not* cover

- Migrating an existing single-process install's *current* state
  (`users.json`, `session_secret.key`, the active `settings.yml`) into the
  shared state directory - that's a one-time manual copy, do it before
  starting `xeonis-gateway.service` for the first time so existing
  accounts/sessions aren't silently abandoned.
- TLS - put a real reverse proxy in front of the gateway for that; it
  isn't this project's job to terminate TLS itself.
