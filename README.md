# TopologyOperator

A GUI tool for *operating* topologies already deployed by
[TopologyBuilder](https://github.com/HTL3R-Xeonis/TopologyBuilder): a
GNS3-like canvas, in-window access to each device's console/screen,
automated per-device configuration push from a second config file, and
a portable `.topoproj` bundle format for exporting/importing a whole
project (topology + configs + canvas layout + drawings) in one file. A
separate tool from TopologyBuilder itself, depending on it as a library
rather than duplicating its ESXi/GNS3 connection code.

**Status: feature-complete and verified end-to-end against real
infrastructure** - GNS3 console embedding, VM screens (direct VNC),
config-push, project import/export, multi-host switching, design mode,
Generate via LLM, packet capture, and the template-outage degrade path
have all been exercised live, not just in the test suite. The GNS3 VM
Deploy feature (below) is thoroughly unit-tested but not yet exercised
against a real OVA import from the sister template API.

## Architecture

- Web app: a FastAPI backend (`app/`) + a plain HTML/JS frontend
  (`app/static/index.html`, `styles.css`, `app.js`, and `projects.js`), not a native desktop app.
- Depends on TopologyBuilder as an installed library
  (`ESXiConnection`/`GNS3Connection`/`Settings`/`Graph`), not a copy of
  its code. Its top-level package name is `src`, so this project's own
  code lives under `app/` specifically to avoid a namespace collision.
- Landing page (`GET /`): a grid of clickable host tiles, one per
  uploaded settings.yml profile (`app/settings_profiles.py`) - Settings
  is a process-global, load-once class, so switching hosts re-execs the
  whole server process against the newly chosen file rather than
  swapping config in place. The canvas for whichever host is active
  lives behind this, reachable via a tile click and back via the
  menubar's own Home button.
- GNS3 console embedding: `xterm.js` in the browser, bridged to the
  node's real Telnet console port via a backend WebSocket proxy
  (`app/console.py`).
- VM screens: a direct VNC server enabled on the VM itself
  (`RemoteDisplay.vnc.*`), bridged the same way (`app/vm_screen.py`),
  rendered in the browser with open-source `noVNC`.
- Config push: a second YAML file (raw CLI commands per device, not a
  structured schema - a uniform "send this verbatim" transport works
  across the whole heterogeneous device mix without a per-vendor
  renderer), pushed via Netmiko (GNS3 console devices) or `pyvmomi`
  guest-ops (ESXi VMs), the same transports the interactive
  console/screen features use. Stops on the first failed command per
  device, but continues to the next device (`app/config_push.py`).
- Project import/export (`app/project_bundle.py`): a `.topoproj` bundle
  (a plain zip) carries the topology YAML, a sanitized settings
  snapshot (used only as an ESXi/GNS3-target safety check on import,
  never auto-applied), the last-pushed device configs, canvas drawings,
  and node canvas positions. Import runs as a background job
  (`app/import_jobs.py`) with an approximate live-polled progress
  percentage, since a deploy can take minutes and TopologyBuilder's own
  deploy has no progress-callback hook. A bundle can opt itself into
  running its own config-push automatically right after the deploy
  succeeds (`manifest.json`'s `auto_config_push`).
- Node canvas positions (`app/layout.py`) are a TopologyOperator-owned
  concern, independent of GNS3's own node x/y - saved from drags, a
  bundle's own layout.json, or a shared force-directed default
  (`src.graph.layout.compute_node_positions`) for whatever neither of
  those covers.
- **Design mode** (`app/design.py`): an offline sketch topology, reachable
  from the landing page independent of whichever host is active - build a
  topology through the same canvas UI before any real host/deploy
  exists, then hand it off via `GET /design/export` (a host-agnostic
  `.topoproj`, importable later against a real host through the normal
  `POST /project/import`). Pure in-memory data with no live GNS3/ESXi
  connection, but autosaved to `design_state.json`
  (`app/design_store.py`) after every edit and restored at startup, so a
  process restart doesn't lose an in-progress sketch - and restoring
  never touches the template service (see the graceful-degrade point
  below), since the sketch's already-resolved node environments are
  saved alongside it rather than re-derived.
- **Generate via LLM** (`app/generate_jobs.py`): a landing-page action
  that turns a natural-language prompt into a topology via the Topology
  Generator API (`src.connections.topology_generator_client`), mirroring
  TopologyBuilder's own CLI `generate` command's retry-until-valid loop,
  then opens the result straight into design mode instead of writing a
  file. Runs as a background job (`POST /design/generate` → `job_id`,
  `GET /design/generate/{job_id}` polls attempt/status/warnings). When every
  attempt is rejected, the final error names the last validation problem
  (e.g. an interface reused on a switch) instead of only "no valid topology". Every
  generated node gets a real position via the same force-directed layout
  algorithm (`src.graph.layout.compute_node_positions`) a deployed
  topology's own missing positions already fall back to, rather than
  landing all nodes on top of each other.
- **Detailed application logging with a hard retention limit**
  (`app/logging_setup.py`, `app/log_store.py`, `app/request_logging.py`):
  the log file at `Settings.LOG_FILE_PATH` is written at DEBUG and keeps
  **at most 100 000 lines** - the current file plus one previous file
  (`<name>.1`) of at most 50 000 lines each; when the current file is full it
  replaces the previous one, so the oldest lines simply fall away (a rename,
  no big file is ever rewritten). Every line reads `time | LEVEL |
  file:function:line - message`. What is logged: the configuration at
  startup (never a credential), every HTTP request and WebSocket session
  (method, path, status, duration, client - never query strings, headers or
  bodies; polling reads at DEBUG, state changes at INFO, 4xx WARNING, 5xx and
  exceptions ERROR with a traceback written without variable values), live
  node/link changes, node start/stop/reload, packet captures, config pushes
  (per device - never the commands or their output), import/generate/GNS3-VM
  jobs, health checks and orphan deletions, plus everything uvicorn and the
  libraries report. Read it from the landing page's **View Logs** link (next
  to View License; `/logs.html`: level filter, search, up to the whole log,
  auto-refresh, download) or from View > Logs inside a project. Server file
  paths are redacted in everything sent to the browser. Older size-rotated
  files from before this scheme (`app.<timestamp>.log`) are not touched - delete
  them by hand if they are in the way.
- **Template service status** (`app/template_health.py`): when the service
  that tells TopologyBuilder which images exist is down or lists nothing,
  every topology fails to validate with "Image X not found" - which points at
  the topology although the cause is elsewhere (it happened when the ESXi
  password changed and the service could no longer log in). The app now asks
  both template APIs (and the GNS3 one's own `/api/servers`, which states
  why a server is unreachable) and shows the problem in a banner on every
  screen (`GET /templates/health`); "Image ... not found" errors get the
  likely cause appended.
- **Wireshark-style packet capture** (`app/capture.py`): start/stop a
  capture on any live link and view it as a live, in-browser
  Wireshark-style packet table (click a link on the canvas) - a
  client-side pcap parser decodes Ethernet/ARP/IPv4/TCP/UDP/ICMP frames
  from the raw stream GNS3's own controller API exposes, with a
  click-to-expand protocol-detail tree and hex dump per packet. The
  equivalent real-Wireshark command
  (`curl -sN <origin>/links/.../capture/stream | wireshark -k -i -`) is
  always shown as plain selectable text for anyone who wants the actual
  external tool - never behind a clipboard-dependent popup, since
  `navigator.clipboard` needs a secure context (HTTPS or localhost) this
  app doesn't always have on a typical plain-HTTP LAN deployment.
- **Graceful degrade when the template-listing service is down**: if
  `load_graph()` can't reach it at startup, `app.state.graph` becomes
  `None` instead of the whole process refusing to start - the real
  error surfaces via the existing warning banner (with a "Retry" button,
  `POST /reload`), and only the live topology view is unavailable; the
  landing page, design mode, and the Logs panel all keep working
  regardless, since none of them touch `app.state.graph`.

- **Health check and orphan cleanup** (`app/maintenance.py`): File menu >
  "Health Check…" runs TopologyBuilder's structural `verify` against the
  live lab (GNS3 nodes started, ESXi VMs powered on, trunk NIC, port groups,
  links) and, if anything fails, offers its curated low-risk repairs (start
  stopped GNS3 nodes, power on stopped ESXi VMs, create missing port groups -
  never a redeploy of a genuinely missing VM/node). "Find Orphans…" lists
  live GNS3 nodes and tool-created ESXi VMs that no node in the topology
  file describes anymore - what a partially failed live delete leaves behind
  - and can delete them after a second, explicit confirmation (irreversible).

## GNS3 VM Deploy

Replaces the configured GNS3 VM with a freshly imported one from its own
OVA (`src.gns3_vm_deploy.deploy_fresh_gns3_vm`, in TopologyBuilder) -
available two ways:

- **On demand**, via File menu > "Deploy Fresh GNS3 VM…" - e.g. to
  recover from a corrupted GNS3 VM without restarting this whole
  process.
- **Automatically at startup**, but only when the configured VM is
  found *genuinely absent* - never just because it exists but isn't
  reporting an IP yet (e.g. still booting).

Requires an OVA resource selected from the sister template API and
`esxi.gns3_vm_mgmt_network`. The browser receives only the template ID and safe
display metadata. The backend streams `/api/download/{id}` into a private
temporary file for the import and deletes it afterward.

An existing same-named VM is always powered off and renamed as a
timestamped backup first, never touched destructively by default. Two
further options, both configured/confirmed explicitly given their real
consequences:

- **Delete the old VM permanently** instead of leaving it as a backup -
  a second, separate confirmation in the GUI (irreversible, so it's
  never bundled into the main "Deploy" confirmation), or `delete_old_vm`
  on `POST /gns3-vm/deploy`. Only ever deletes the old VM *after* the
  new one has confirmed itself alive by reporting an IP - a new VM that
  never comes up leaves the old one untouched, exactly as if this
  option had been off.
- **Pin the VM's MAC address to a configured range** -
  `esxi.gns3_vm_mac_range: [start_mac, end_mac]` in `settings.yml`, e.g.
  so a DHCP server can be set up to only hand out IP addresses to MACs
  in that range. If the old VM's own MAC already falls in the range,
  it's kept as-is (preserves the same DHCP-leased IP across a
  redeploy); otherwise a free MAC from the range is picked, avoiding
  every other VM currently on the ESXi host.

A missing trunk network (`esxi.trunk_port_group`) is auto-created on a
genuinely fresh ESXi host, the same way a normal topology deploy already
self-heals it. A missing *management* network is not auto-created -
that's an externally-managed network this tool has no safe way to guess
settings for, so it still fails loudly.

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp settings.example.yml settings.yml   # configure the ESXi/GNS3 host
cp .env.example .env                    # fill in ESXI_PASSWORD/GNS3_PASSWORD
export TOPOLOGYOPERATOR_SETTINGS=settings.yml
uvicorn app.main:app --reload
```

Open `http://localhost:8000/` (or wherever it's bound) - the landing
page lists whatever settings.yml profiles have been uploaded via the
UI's own "Add host" tile; the file `TOPOLOGYOPERATOR_SETTINGS` points
at on startup shows up there too, already marked active, so a first run
has something to click into immediately.

## Host configuration designer and settings

Choose **Design a host configuration** on the home screen to build a host
profile. The whole form is one scrollable page (a row of buttons at the top
jumps to a section): General, ESXi host, Networking, GNS3, API services and
Credentials cover 22 host/runtime settings plus both passwords, with
TopologyBuilder defaults filled in and automatic options left blank.
The generated YAML at the bottom updates while you type. **Download YAML**
saves a local copy; **Create host** adds a named profile without activating it
or deploying anything. Existing filenames are rejected rather than overwritten.

Each host tile's **Settings** button opens the same page for an existing
profile. Optional values, booleans, ports, protected port-group lists, and MAC
ranges have dedicated inputs and validation.

**Passwords** can be pasted into the Credentials section. The settings loader
does not accept passwords in YAML, so they are stored apart from it, per host, in
`<profiles dir>/.secrets/<profile>.json` (mode 0600) and applied at startup
(they win over `ESXI_PASSWORD`/`GNS3_PASSWORD` for that host; those variables
still work when nothing is stored). They are write-only: the server only reports
*whether* one is stored, an empty field keeps it, and they never appear in the
YAML, the preview, the Test connections request or the log. Deleting a host
deletes its stored passwords; a changed password takes effect after
Apply / restart.

**Test connections** uses the unsaved form values from the server. Each service
shows its address, check type, elapsed time, status, and diagnostic message.
ESXi and GNS3 checks test TCP reachability; API checks request the base URL.
An HTTP 404 means the server responded but has no base-path route; these checks
do not authenticate or run generation/deployment. GNS3 is skipped until its VM
address is known for the selected host.

**Save settings** retains unexposed YAML settings but removes YAML comments.
**Apply / restart** activates the saved profile through the existing restart flow;
open consoles disconnect. The active settings file is included even when it
lives outside the uploaded profiles directory.

## Selecting a project independently

A host profile does not need a topology file. Use **Project** on its tile to
choose a named project from the library and optionally override its GNS3 project
name. The home screen's **Project library…** button creates empty projects or
uploads topology YAML (up to 2 MB), and downloads their YAML again. Uploaded
files are validated with TopologyBuilder's structural checks without contacting
host template services; template availability is checked at activation.

The application stores each project in `projects/<internal-id>/`, with its display
name in `project.json` and topology in `topology.yaml`. Override this directory
with `TOPOLOGYOPERATOR_PROJECTS_DIR`; include it and the selection store in backups.
Names never become filesystem paths. Duplicate names are allowed as distinct projects.
The project controls live in the separate `app/static/projects.js` feature file.

Open a library project in the design canvas to edit its topology, drawings, and
node positions. **Save Project** writes those parts back to that project only;
each library project therefore retains its own canvas layout. **Deploy to Host…**
shows the target ESXi address, GNS3 VM, node/link totals, role totals, and whether
the host uses incremental or full deployment. The preview is read-only. Starting
deployment selects the project for that host; if the host or project is not active,
the app restarts once and resumes the confirmed deployment afterward.

Choose **No project selected** to clear the selection. Save the selection, then
use **Apply / restart** (or activate that host later). Selecting a project
does not deploy it; importing a `.topoproj` remains an explicit deployment.

Selections live in `project_selections.json`, keyed by host profile filename, and
survive restarts. Override the store path with `TOPOLOGYOPERATOR_PROJECT_SELECTIONS`.
The same topology can be selected for different hosts. Host YAML downloads contain
no topology path. TopologyBuilder's global settings
are populated from the selection only at startup, so project changes use the
same restart flow as host changes. Import and live edits require a selected project.

## Endpoints

- `GET /projects`, `POST /projects` (name plus optional topology_yaml),
  `GET /projects/{id}/download`: managed project library, with no deployment.
- `POST /design/project/{id}/open`, `PUT /design/project/{id}`: open and save a
  library project in the offline design canvas.
- `GET /projects/{id}/deploy-preview/{host}`, `POST /projects/{id}/deploy/{host}`:
  inspect and start a library deployment. The start endpoint requires that host
  and project to be active after any required restart.
- `GET/PUT /settings-profiles/{filename}/project`: independent host selection;
  PUT accepts a managed library `project_id` only.
- `GET/PUT /settings-profiles/{filename}/connections`, `POST /host-config/create`:
  host settings; the optional `secrets` body field (`esxi_password`,
  `gns3_password`) is write-only - GET returns `passwords_set` (booleans) only.
- Browsers navigating to an unknown address, a wrong method or a failing page get
  a readable 404/405/5xx page (`app/error_pages.py`); requests that don't ask for
  HTML (fetch, curl) keep getting the JSON `{"detail": ...}` body. There is no
  separate 403 page - nothing here is access-controlled.
- `GET /ova-templates`: safe OVA resource metadata proxied from the sister API;
  filenames, source directories, credentials, and server paths are omitted.

- `GET /health` (includes `warning` - see the graceful-degrade point
  above), `POST /reload` (manual retry once a down dependency recovers),
  `GET /topology` (static graph, 503 while `app.state.graph` is down),
  `GET /nodes` (graph merged with live GNS3/ESXi state), `GET/POST
  /layout`.
- `GET /templates`, `POST /nodes`, `DELETE /nodes/{name}`, `POST
  /links`, `DELETE /links/{node1}/{node2}` - live add/remove.
- `POST /nodes/{name}/start`, `.../stop`, `.../reload` - node lifecycle.
- `GET /links` (every live link + capturing state), `POST
  /links/{node1}/{node2}/capture/start`, `.../capture/stop`, `GET
  .../capture/stream` (proxies GNS3's own live pcap stream) - see
  Architecture above.
- `POST /config-push`, `GET/POST /drawings`.
- `GET /logs?lines=N` (the in-project panel), `GET /logs/entries?lines=N&level=L&search=S` (entries with
  tracebacks kept together, plus how full the log is), `GET /logs/download` (the whole retained log as text),
  `GET /templates/health` - see Architecture above.
- `GET /project/export`, `POST /project/import` (kicks off a background
  job, returns `{job_id, total}`), `GET /project/import/{job_id}`
  (progress + final result), `POST /project/reload-gns3` (closes and
  reopens the live GNS3 project, restarting whichever nodes were
  running - refreshes GNS3's own state without redeploying anything).
- `GET/POST /settings-profiles`, `DELETE /settings-profiles/{filename}`,
  `POST /settings-profiles/{filename}/activate` (see Architecture above).
- `GET /design/topology`, `POST /design/nodes`, `DELETE
  /design/nodes/{name}`, `POST /design/links`, `DELETE
  /design/links/{node1}/{node2}`, `GET/POST /design/layout`, `GET/POST
  /design/drawings`, `POST /design/reset`, `GET /design/export`, `POST
  /design/generate`, `GET /design/generate/{job_id}` - design mode, all
  mirroring the equivalent live endpoint above (see Architecture above).
- `POST /maintenance/verify` (`{"fix": bool}`; returns the repairs applied
  and every check's result), `GET /maintenance/orphans`, `POST
  /maintenance/orphans/delete` (`{"orphans": [{"type": "gns3_node" |
  "esxi_vm", "name": ...}]}`, permanent) - see Architecture above.
- `POST /gns3-vm/deploy`, `GET /gns3-vm/deploy/{job_id}` (see GNS3 VM
  Deploy above).
- `WS /ws/console/{node}`, `WS /ws/vm-screen/{node}`.

The frontend at `/` covers all of these.

## Testing

```bash
pip install -e ".[test]"
pytest
```

377 tests, all offline (mocked ESXi/pyvmomi, real local TCP servers
standing in for GNS3 consoles/Netmiko targets - no real infrastructure
needed).

CI (`.github/workflows/ci_pipeline.yml`) runs this suite plus
`ruff check`/`ruff format --check` on every push. A few rule categories
are deliberately disabled project-wide (see `[tool.ruff.lint]` in
`pyproject.toml`) rather than suppressed inline everywhere: blind
`except Exception` and `try`/`except`/`pass` are a real, pervasive,
commented-at-each-call-site best-effort pattern throughout this
codebase, not oversights; nested `with` blocks in tests are a common,
defensible readability idiom ruff can't safely auto-combine anyway.

## License

GNU GPLv3 - see [LICENSE](LICENSE).
