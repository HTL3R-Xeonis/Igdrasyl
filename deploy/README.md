# Phase 3 deployment: gateway + per-profile backends

**This describes the cutover from today's single-process app to Phase 3's
gateway/backend split. That cutover has not happened yet** - see
`app/main.py`'s own docstring and `TopologyBuilder/handoff/plans/
PLAN_login_multiserver.md`. Everything here is ready to use once the
decision is made to actually do it; it does not change how the app runs
today.

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
resolve against whatever `WorkingDirectory=` happens to be - fine for one
process, actively wrong the moment there's more than one. Get this wrong
and each backend silently bootstraps its **own** separate `admin`/`admin`
account instead of sharing the one install-wide store, and nobody notices
until two users on two different profiles can't see each other's accounts.

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
   `enable-linger` is required - without it, `--user` units stop the
   moment nobody is logged in as `xeonis`, which on a headless box is
   always.

2. **The shared state directory**, owned by that account:
   ```bash
   sudo -u xeonis mkdir -p /opt/ginnungagap/state/settings_profiles /opt/ginnungagap/state/logs
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
`TOPOLOGYOPERATOR_SETTINGS`, then `exec`s uvicorn - no extra process stays
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

## What this does *not* cover

- Migrating an existing single-process install's *current* state
  (`users.json`, `session_secret.key`, the active `settings.yml`) into the
  shared state directory - that's a one-time manual copy, do it before
  starting `xeonis-gateway.service` for the first time so existing
  accounts/sessions aren't silently abandoned.
- TLS - put a real reverse proxy in front of the gateway for that; it
  isn't this project's job to terminate TLS itself.
- The actual decision to cut over a live, currently-single-process
  install - see `app/main.py`'s own docstring and the Phase 3 plan.
