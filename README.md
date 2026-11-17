# Ginnungagap

*(The project, repository and Python package are still called TopologyOperator, as are the `TOPOLOGYOPERATOR_*`
environment variables; "Ginnungagap" is the name shown in the app. In Norse myth it is the void between ice and fire
from which the world arises - here, the empty canvas from which a lab arises.)*

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
Generate via LLM, packet capture, the template-outage degrade path, and
the GNS3 VM Deploy feature (below, including a real OVA import through the
sister template API and a full project redeploy onto the resulting VM)
have all been exercised live, not just in the test suite.

Start-up does not need the ESXi host: the session is opened on first use (`app/esxi_session.py`), so a host that is down
or not set up yet still lets the app start (landing page, design mode, host settings); the host's calls answer 503 with the
reason until it is reachable, and the app connects by itself afterwards (10 s between attempts).

## Architecture

- Web app: a FastAPI backend (`app/`) + a plain HTML/JS frontend
  (`app/static/index.html`, `styles.css` and several scripts), not a native desktop app.
- **Layout of the backend**: `app/main.py` only wires things together (middleware, error handlers, the WebSocket routes, the
  frontend mount). The FastAPI instance is in `app/application.py`, the startup (topology and GNS3 project resolution, retry
  loop, lifespan, banner state) in `app/startup.py`, and the HTTP endpoints in `app/routes/`, one module per area: `system`
  (health, reload, logs, template status), `auth` (login/logout/`GET /me`), `admin` (admin-only user accounts, AI
  configuration policy, log summaries, audit log), `live` (topology, nodes, layout, labels, notes, links, capture, serial, start/stop),
  `staging` (review/apply staged live edits), `push` (config push), `design` (the offline design canvas and canvas annotations), `projects` (bundles, project library,
  hold/resume, saved configs, deploy), `maintenance` (health check, connectivity test, orphans, GNS3 VM deploy) and
  `host_settings` (settings profiles, host configuration, connections, OVA templates). `app/main.py` imports the route
  modules in a fixed order, because Starlette tries routes in registration order.
- **Layout of the frontend**: no bundler. The scripts are plain classic scripts that `index.html` loads in a fixed order and
  that share one global scope: `core.js`, `canvas-state.js`, `label-placement.js`, `drawings.js`, `canvas-render.js`,
  `edit-actions.js`, `link-actions-and-capture.js`, `palette-and-screenshot.js`, `import-and-generate.js`,
  `connection-settings-and-maintenance.js`, `consoles-and-push.js`, `projects.js`, `staging.js`, `snapshots.js`,
  `info-popover.js`, then `user-menu.js` (each starts with a comment saying what
  it holds). Because of the shared scope the order matters only for code that runs while a file loads; a function defined in a
  later file can be used by a callback at any time. Tests that look at the frontend read all of them in load order
  (`tests/frontend_js.py`).
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
- **Tabs out of the panel**: every console/screen tab can be dragged out of the Console/Screen panel with the left mouse
  button (move at least 8 px, release more than 24 px outside the panel, Escape cancels) to open just that session in its
  own browser window; the **Pop out** button at the right end of the tab row still moves the whole panel (tabs and
  sessions - the side panel then shows only the device list, and everything else keeps working while it is open). Holding **Shift**
  (when releasing the dragged tab, or clicking Pop out) opens a new browser *tab* instead of a window. Careful with the tab:
  the terminal and the screen draw with the opening page's animation frames, which a browser pauses while that page is a
  background tab, so a popped-out tab can show a frozen picture; a window does not have that problem. Each tab shows
  the device name and, beside it, the connection state (a failure in red, the full text as tooltip). The live terminal/screen is moved, not copied, so
  the connection runs on; closing the window brings the session back into its tab, closing the session closes its window,
  and a popped-out tab is marked with an arrow and focuses its window when clicked. The browser must allow the pop-up
  (the drag release counts as the user gesture); otherwise a notice says so and nothing is moved.
  Dragging a tab sideways along the row changes the order of the tabs (in the panel and in the pop-out window alike).
  A popped-out session has a **Pop in** button in its window, and the whole-panel button reads **Pop in** while the panel
  is out; both put the live view back (closing the window does the same). **Settings > Open consoles in their own window**
  (remembered in the browser) makes a console, screen or serial line that is opened from a device's menu appear straight in a
  window of its own instead of a tab.
- **Serial console of an ESXi VM** (`app/serial_console.py`): once a VM has its network serial port (see "Serial console
  agent" below), its popup's **Serial console** entry opens the serial line in the same terminal panel
  (`/ws/serial/{name}`). The ESXi serial port takes one client at a time, so a second console of the same VM is refused,
  and a running serial config push (or another console) blocks the connect - the tab then says so. It needs the VM to
  be powered on; if the screen stays empty, press Enter. (An earlier interactive serial console was removed on
  2026-09-22; this one reuses the port setup the config push brought back.)
- VM screens: vSphere's own built-in WebMKS console-ticket API (not a
  VM-side `RemoteDisplay.vnc.*` setting - that approach was tried and
  hit a dead end on this project's infrastructure, see
  `app/vm_screen.py`'s own docstring), whose transport already carries
  raw RFB/VNC protocol bytes, bridged the same way (`app/vm_screen.py`),
  rendered in the browser with open-source `noVNC`.
- Config push: a second YAML file (raw CLI commands per device, not a
  structured schema - a uniform "send this verbatim" transport works
  across the whole heterogeneous device mix without a per-vendor
  renderer), pushed via Netmiko (GNS3 console devices) or `pyvmomi`
  guest-ops (ESXi VMs), the same transports the interactive
  console/screen features use. Stops on the first failed command per
  device, but continues to the next device (`app/config_push.py`).
- **Export** contains **Diagram (PNG)** and **Diagram (SVG)** in both live and design mode. Both cover the
  whole canvas, including links and drawings, regardless of zoom or pan. The SVG embeds the device icons
  and can be opened offline. **Operator Project (.topoproj)** exports the existing Operator bundle.
- **GNS3 Project (.gns3project)** streams the live controller's native archive without bundling base images
  or ESXi VMs. GNS3 requires the project's devices to be stopped before export; the Operator never stops
  them for you. Use `.topoproj` to hand off an Operator topology with ESXi device definitions.
- Project import/export (`app/project_bundle.py`): a `.topoproj` bundle
  (a plain zip) carries the topology YAML, a sanitized settings
  snapshot (used only as an ESXi/GNS3-target safety check on import,
  never auto-applied), the last-pushed device configs, canvas drawings,
  and node canvas positions. Live import and export require an active host
  profile. Import runs as a background job
  (`app/import_jobs.py`) with an approximate live-polled progress
  percentage, since a deploy can take minutes and TopologyBuilder's own
  deploy has no progress-callback hook. The status also carries
  `elapsed_seconds`, `phase` ("creating devices", then "finishing (network
  links, VLAN setup, checks)") and `pending` (the devices not created yet),
  so the menubar shows e.g. "3:05, still to create: WINSRV" and never reads
  100 % while the job still runs - a big Windows image makes the bar stand
  still for minutes. A bundle can opt itself into
  running its own config-push automatically right after the deploy
  succeeds (`manifest.json`'s `auto_config_push`). Verified live against a
  real, full (non-incremental) import: the bar, clock and phase text all
  update correctly. That same test also found TopologyBuilder's own
  deploy call could hang indefinitely if the ESXi/GNS3-VM connection died
  mid-deploy (no timeout anywhere in that path) - fixed there
  (`ESXiConnection`/`GNS3VMInterfaceSetup` now bound every task wait and
  SSH read), so a future import fails with a clear error instead of
  hanging forever.
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
  file. You can attach up to five PDF, PNG, JPEG, Word (.docx), TXT, Markdown
  or CSV files (10 MiB total) with an optional prompt. When you attach exactly one
  PNG or JPEG screenshot, select **Match screenshot layout** to place device icons
  according to their positions in the image. This requires a vision model and
  an updated Topology Generator. Local text models can use extracted text and OCR
  for ordinary attachment generation. Runs as a background job
  (`POST /design/generate` or `POST /design/generate/attachments` → `job_id`,
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
  auto-refresh, download, Clear logs) or from View > Logs inside a project (also with Clear logs). Server file
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
  screen (`GET /templates/health`). It points to credentials when the
  collector reports a login problem and to connectivity for other ESXi
  connection failures. "Image ... not found" errors get the
  likely cause appended. When the service cannot be reached at all, the
  startup banner and the 503 answers of `/templates`, `/design/templates`,
  `/design/nodes` and `/topology` name the service and the reason ("The ESXi
  template service (host) is not reachable: connection refused or host
  down.") instead of a raw connection error; design mode keeps working.
  **Self-healing**: when a project import is refused with "Image ... not found"
  because the service lists too little (a collector that hung, a GNS3 VM that
  was just restarted), the import job asks both services to read again, waits up
  to 150 s until they are healthy (`template_health.heal`) and repeats the import
  once - the check runs before anything is deleted, so this is safe. A missing
  image on a healthy service is reported at once. The collectors on the service
  host stop a run after 120 s (`TimeoutStartSec`) and cut ESXi connections that
  stop answering (60 s), so a host that goes down mid-run cannot block the
  timer for hours (see Docker-Server-Files).
- **Robustness** (`tests/test_route_robustness.py`): every route is called with garbage bodies and odd path values
  while ESXi and GNS3 are unreachable, while no host is active, and while a fake GNS3 answers 500, 404 or non-JSON
  everywhere; none may answer 500 (deliberate refusals are 4xx/502/503). A random sequence of design operations with
  odd node names must not break either. Found and fixed this way: `GET /nodes` and `GET /links` answered 500 when the
  GNS3 VM was down (now 503, the page already copes), `POST /design/project/{id}/open` with an unknown id,
  `POST /gns3-vm/start` with ESXi unreachable. **Node names** typed in (design canvas and live Add node) must be 1-63
  characters of letters, digits, `-` and `_`, starting with a letter or digit, and unique ignoring case
  (`app/node_names.py`) - names like `../x`, `a b` or `r1` next to `R1` used to be accepted and broke the deploy or a
  URL later. `GET /design/layout` gives nodes nobody placed (a design created by a script or opened from a stored project)
  a force-directed default position instead of piling them on one spot.
- **Parallel links**: two nodes can be linked more than once, over
  different interfaces (the only rule is that an interface carries one
  link: "Interface Gi0/1 is used twice"). The canvas draws every link on
  its own, side by side, each with its own port labels, and a link is
  addressed by node *and interface* on both ends: `DELETE /links/{a}/{b}`,
  `PUT /links/{a}/{b}/options`, the capture endpoints and
  `DELETE /design/links/{a}/{b}` take optional `?if1=..&if2=..` (interface
  on `a` / on `b`); without them the first link of the pair is meant, as
  before. `GET /topology` and the design view list each node's `links` as
  `{interface, peer, peer_interface}` next to the older `interfaces` map.
  If the interfaces match no live link, the request fails instead of
  hitting the link that does exist. `verify` checks each link by its ports,
  and TopologyBuilder creates each one in GNS3 by its own interface pair.
- **Automatic label placement**: port labels you have not moved are placed on the first free spot next to their link endpoint (text
  grows away from the node; nudged outward/sideways around icons, names, other labels and the lines of *other* links;
  lying on its own line is fine), so a switch with five links no longer
  stacks them. Pure geometry in `label-placement.js` (`placePortLabels`), run under `node` by `tests/test_label_placement.py`; moved labels stay
  exactly where they were put and count as obstacles for the others.
- **Movable labels**: port labels and node names are dragged with the mouse (double-click puts one back; View menu >
  Reset Label Positions resets all). They sit in a layer above the nodes, so no icon hides one, and keep a halo so
  overlapping text stays readable. What is stored is the offset from the default spot (`app/label_layout.py`,
  `GET/POST /labels`, `label_layout.json`, git-ignored), so a label follows its node; the design sketch keeps its own
  copy in the design state, which makes label moves undoable there and keeps them in a saved library project. A
  `.topoproj` export carries them as `labels.json`, and an import applies them (replacing the saved set, like the node positions).
- **Node notes** (`app/node_notes.py`): click a node, then **Note** (the button reads *Edit note* once there is one) for a
  free-text note per node (up to 20 000 characters, Ctrl+Enter saves); a node with a note shows a small sticky note at its
  icon. The dialog also lists the node's **default login** read from its template - an ESXi template's `users` and
  `passwords`, a GNS3 template's `usage` text - read-only, the password hidden until *Show*; nothing of it is stored or
  logged (`GET /nodes/{name}/credentials`, `app/node_credentials.py`). Notes live in `node_notes.json` (git-ignored; the
  design sketch keeps its own copy, which makes them undoable there and keeps them in a saved library project) and travel
  in a `.topoproj` export as `notes.json` (an import replaces the saved set). Endpoints: `GET /notes`,
  `PUT /notes/{name}` and the `/design/...` twins.
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
  regardless, since none of them touch `app.state.graph`. The app also
  **retries by itself** in the background (15s, 15s, 30s, 30s, then every
  60s, never while another change to the lab runs or a project is on hold)
  until the warning is gone - after a host reboot nobody has to press Retry or
  restart the process once the GNS3 VM has finished booting. It never replaces
  the GNS3 VM on its own (that stays with the startup check and the File menu).

- **Health check and orphan cleanup** (`app/maintenance.py`): File menu >
  "Health Check…" runs TopologyBuilder's structural `verify` against the
  live lab (GNS3 nodes started, ESXi VMs powered on, trunk NIC, port groups,
  links) and, if anything fails, offers its curated low-risk repairs (start
  stopped GNS3 nodes, power on stopped ESXi VMs, create missing port groups -
  never a redeploy of a genuinely missing VM/node). Missing VLAN
  subinterfaces on the GNS3 VM (lost on its reboot, so links to ESXi VMs carry
  no traffic) are never part of those repairs: re-creating them restarts the
  Cloud nodes, so the dialog asks for it separately
  (`POST /maintenance/verify` with `fix_vlan_subinterfaces`). "Find Orphans…" lists
  live GNS3 nodes and tool-created ESXi VMs that no node in the topology
  file describes anymore - what a partially failed live delete leaves behind
  - and can delete them after a second, explicit confirmation (irreversible).
- **Suggest fix** appears on failed Health Check rows that identify a device or link. Pick a target and
  optionally describe your goal. The Operator sends the failed check, target role and image, and adjacent
  links to the configured Topology Generator. Review its explanation, warnings and commands before
  choosing **Add to Config Push**. This only appends to the editor; it preserves other devices and target
  options and never saves or pushes. Some structural checks can only be addressed in the topology or
  hypervisor, so the generator can return advice without commands.
  This feature requires the generator to implement `POST /api/suggest-fix`; older generators report that
  suggestions are unavailable. The existing automatic Health Check repairs are independent of this feature.
- **Connectivity test** (`app/connectivity_test.py`): File menu > "Connectivity Test…" puts real traffic on the lab
  network, which the structural health check does not. The VMs that share one layer-2 segment (a switch or a direct link)
  get a temporary address each from 198.18.0.0/15 (one /24 per segment), every one pings every other, and the addresses
  are removed again - also when something fails on the way. VMs behind a router are not tested against each other (they
  would fail for lack of routes, not because of the wiring) and are listed as skipped. It runs through the config-push
  engine, so it needs VMware Tools; on Linux the interface is taken away from NetworkManager for the test (it removes
  hand-set addresses otherwise), on Windows an ICMP firewall rule is added and removed. The clean-up puts back only what the setup changed (a state file in the VM records
  it): a Windows adapter that was on DHCP goes back to DHCP, one with a fixed address keeps it, and a Linux interface that was down is
  switched off again. API:
  `POST /maintenance/connectivity-test` (`{"nodes": [...], "count": 10, "size": 56}`, all optional) returns a `job_id`;
  `GET /maintenance/connectivity-test/{job_id}` shows `phase`, then the `pairs` (sent, received, loss, average round trip),
  `skipped`, `problems` and `all_ok`. Only one such test or config push runs at a time. Measured on the `big-lab-13`
  lab: 64-byte pings run without loss up to at least 200 per second, but 1400-byte pings above about 50 per second
  (roughly 0.6 Mbit/s per direction) lose packets - the emulated path (QEMU switch image inside the GNS3 VM), not the ESXi
  host, is the limit. Measured on the GNS3 VM under load (`spikes/gns3_vm_load_probe.py`): the single vCPU of the switch node
  `SW1` (Cisco `vios_l2`) sits at 99.9 % while the VM as a whole is still a third idle; KVM and nested virtualization are active.

## GNS3 VM Deploy

Replaces the configured GNS3 VM with a freshly imported one from its own
OVA (`src.gns3_vm_deploy.deploy_fresh_gns3_vm`, in TopologyBuilder) -
available two ways. The new VM always gets **4 vCPUs and 8 GB RAM** (set after the
import, before it is powered on), whatever the OVA declares - the stock OVA's
1 vCPU / 2 GB cannot boot a handful of IOSv nodes:

- **On demand**, via File menu > "Deploy Fresh GNS3 VM…" - e.g. to
  recover from a corrupted GNS3 VM without restarting this whole
  process.
- **Automatically at startup** on an active host. The Operator looks for
  the configured VM name first, then checks the ESXi inventory for one
  identifiable GNS3 VM. It imports an OVA only when no match exists.

An explicit OVA template and `esxi.gns3_vm_mgmt_network` take precedence.
For a first deployment without those settings, the Operator can select the only
GNS3 OVA from the catalog and reuse a standard port group on the ESXi management
network. It prefers `VM Network` when it shares the same uplink switch and VLAN
as the management VMkernel interface. Select the missing settings in host
settings when discovery is inconclusive. The OVA deploy service imports the
template from its share and needs valid credentials for the target ESXi host.

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
cp .env.example .env                    # machine-wide settings only; host passwords are entered in the host settings
export TOPOLOGYOPERATOR_SETTINGS=settings.yml
uvicorn app.main:app --reload
```

For a long-running instance add `--timeout-graceful-shutdown 10`: without it an old process that is still holding open
console WebSockets can hang in its shutdown and survive a restart for days (seen on the test box, where `kill` had no effect
and only `kill -9` helped).

Open `http://localhost:8000/` (or wherever it's bound) - the landing
page lists whatever settings.yml profiles have been uploaded via the
UI's own "Add host" tile; the file `TOPOLOGYOPERATOR_SETTINGS` points
at on startup shows up there too, already marked active, so a first run
has something to click into immediately.

## Host configuration designer and settings

Choose **Create/Design a Host** on the home screen to build a host
profile. The whole form is one scrollable page: General, ESXi host, Networking, GNS3, API services and
Credentials cover 21 host/runtime settings plus both passwords, with
TopologyBuilder defaults filled in and automatic options left blank.
The generated YAML at the bottom updates while you type. **Download YAML**
saves a local copy; **Create host** adds a named profile without activating it
or deploying anything. Existing filenames are rejected rather than overwritten.

**Default values** of the form come from TopologyBuilder, optionally overridden
by a dedicated file: copy `host_defaults.example.yml` to `host_defaults.yml` (or
set `TOPOLOGYOPERATOR_HOST_DEFAULTS`), keep only the lines to change - same
layout as a host settings file, no passwords. It is read each time the form is
opened (no restart); an invalid file is ignored as a whole and the reason is
logged. Existing hosts still show the real default for anything they don't set.
The GNS3 VM template field prefills itself too, but only on a brand-new host
whose catalog has exactly one GNS3-tagged template - it still has to be typed
(or confirmed/changed) whenever that's ambiguous, and an existing host's own
stored choice is never touched.

The vSwitch and trunk port group fields default to a fixed name
(`internal_network`/`PG_GNS3_TRUNK`) that gets created automatically - by that
name - the first time the GNS3 VM is deployed to this host, so a new host
normally needs no changes there either. The one thing that genuinely can't be
guessed is which existing ESXi network is the **management** network for the
GNS3 VM's first adapter - that has to already exist (it's how the host stays
reachable at all), so picking the wrong one risks cutting off access; it stays
a manual choice.

Each host tile's **Settings** button opens the same page for an existing
profile. Optional values, booleans, ports, protected port-group lists, and MAC
ranges have dedicated inputs and validation.

**Service addresses**: a host profile must name the four services it uses (`api.esxi_template_server_url`,
`api.gns3_template_server_url`, `api.topology_generator_url`, `api.ova_deploy_url`) - or the matching environment
variable must (`ESXI_TEMPLATE_SERVER_URL`, ..., which is how the appliance points everything somewhere else). The built-in
lab addresses of TopologyBuilder are not used as a fallback: activating a profile without them is refused with a message
naming the missing keys (HTTP 400 on the gateway), and a backend started with such a profile stops with the same text in
its log.

**Passwords** can be pasted into the Credentials section. The settings loader
does not accept passwords in YAML, so they are stored apart from it, per host, in
`<profiles dir>/.secrets/<profile>.json` (mode 0600) and applied at startup
(the Operator does not read `ESXI_PASSWORD`/`GNS3_PASSWORD` from the environment any
more). The Operator refuses to open an ESXi session if its password is missing.
Creating the live topology backend requires both passwords. The error names
the missing service and points to Host settings > Credentials.
The ESXi password of the active host also travels
with every OVA deploy request to the deploy service, which has no password list of its
own. They are write-only: the server only reports
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

## Accounts, roles and the admin panel

Every page and API request requires a signed-in session; `app/auth_middleware.py`
redirects HTML requests to `/login` and returns 401 JSON for everything else, with
a short allow-list (`/login`, `/logout`, `/health`, static assets needed to render
the login page). `/login` is a plain HTML form (no JS framework, no browser-native
auth popup) with a "Stay logged in" checkbox - unchecked sessions last 12 hours,
checked ones last 30 days; its background is a blurred photo (`app/static/login-bg.jpg`)
behind a glass-panel card, still styled from `styles.css` so it stays in sync with
the rest of the app's theme. Passwords are hashed with salted PBKDF2-HMAC-SHA256
(600,000 iterations); sessions are HMAC-signed cookies. Both use only the standard
library - no new dependency. Accounts live in `users.json` (gitignored, path
overridable via `TOPOLOGYOPERATOR_USERS_FILE`); the session-signing secret is
generated once into `session_secret.key` next to it and persists across restarts.
(Behind the gateway these files must be absolute shared paths - see `deploy/README.md`; a gateway or managed backend
without them refuses to start.) If TopologyBuilder's test switch `LITERAL_API_VALUES` is set, startup logs a warning
that the template lists are hardcoded and the template services are not asked.
If `users.json` doesn't exist at startup, one `admin` account is created with the
default password `admin` - deliberately predictable, not a generated secret, since
it's meant to be the very first login on a fresh install and changed right away via
the admin panel; logged once at `WARNING` level as a reminder.

Three roles share the same login flow: `admin`, `user` and `readonly`. Regular
users keep full access to everything they always had, destructive actions
included (Find Orphans, Deploy Fresh GNS3 VM, etc. are not role-gated).
Admin-only: creating/deleting/activating a settings profile and editing its
connection settings (`app/routes/host_settings.py`), user account
management, and enabling AI configuration in Config Push - plus, on the
gateway only, explicitly restarting a backend (`POST
/settings-profiles/{filename}/restart`, separate from activate, which any
logged-in user may do there). The right panel's status bar (above the node
list, next to the "Connected: N nodes" text) shows the signed-in username
(`GET /me`). The host overview also has a **Log out** button. Hover over the
canvas username or activate it with the keyboard or a tap to open **Log
out**. Admins also get an **Admin Panel** link to `/admin.html`, a small
standalone page (own JS, not part of the canvas SPA) for user CRUD, the AI
configuration switch, and logs (below).

`readonly` is a blanket, middleware-level block (`app/auth_middleware.py`):
every request whose method isn't `GET`/`HEAD`/`OPTIONS` gets a 403 before it
reaches any route, including anything proxied to a backend behind the gateway - a new
mutating endpoint added later is covered automatically, nothing to remember
per route. The one exemption is picking which backend a readonly user's own
session points at (`POST /settings-profiles/{filename}/activate`) - that's
choosing what to look at, not an infrastructure change. WebSocket console/VM-
screen connections are deliberately **not** blocked for `readonly` - watching
a device's console/screen is fine for an observer; a real "watch but don't
type" split would need new logic in `app/console.py`/`app/serial_console.py`
and wasn't judged worth the complexity for this project.

Each user also has an `allowed_hosts` field (`app/users.py`), default `"ALL"` -
every settings-profile filename, same as before this existed. An admin can
restrict a regular user to a specific list of filenames from the admin panel's
per-user "Allowed hosts" control (click the summary to open a checklist of
every existing profile); `GET /settings-profiles` then only lists the
filenames that user may see. Admins are never restricted by their own
`allowed_hosts` value, regardless of what it's set to - "admin" already means
install-wide access everywhere else in this project. The admin panel also has
a **Bulk add users** section: paste `username;password` lines (one account per
line, split on the first `;` only so a password may itself contain `;` or any
other character), pick a shared role and host restriction for the whole
batch, and import - one bad line (duplicate username, too-short password, a
typo with no `;`) reports an error for that line without stopping the rest
(`POST /admin/users/bulk`).

The admin panel's **Logs** section has an **Export logs** link (just the
existing `GET /logs/download`, open to every signed-in user like the Logs
page's own Download button - not admin-only, so this is a shortcut, not a
restriction) and a **Summarize with AI** button (`POST
/admin/logs/summarize`, admin-only): sends the most recent 2,000 log lines,
path-redacted (`app/public_text.redact_log_text`, same redaction already
used for `/logs/entries`/`/logs/download`), to the configured Topology
Generator's `POST /api/summarize-text` (mirrors `/api/suggest-fix`'s shape -
one best-effort attempt, no internal retry) and shows the plain-text summary
below the button.

Long instructional text that used to sit permanently under a field (Config Push's
command-format notes, the host-config dialog's per-field captions) now collapses
behind a small "?" button instead - `app/static/info-popover.js`, one
document-level delegated click listener shared by every such button.

The admin panel's **Audit log** section (`GET /admin/audit-log`, admin-only)
is a second, much smaller log than the main verbose one: named
security/admin events only (`login_success`/`login_failure`, `logout`,
`user_created`/`user_deleted`/`user_role_changed`/`user_password_changed`/
`user_allowed_hosts_changed`, `ai_snippet_topics_changed`, and, on the
gateway, `host_activate`/`host_restart`) - who did what, without scrolling
through every request. `app/audit_log.py`, same bounded-line-count file
rotation as the main log (`app/log_store.py`'s `LineLimitedFile`), its own
file (`TOPOLOGYOPERATOR_AUDIT_LOG_FILE`, default `audit.log`). A write
failure never breaks the action it's recording - it's logged to the regular
log instead. Backend-side actions (config push, node/link add/remove,
project deploy) are out of scope for this first pass.

On the gateway's "choose a server" page, each tile also shows who's
currently routed to that backend (`app/backend_presence.py`, in-memory only,
a 5-minute window) - a plain count for everyone, and for an admin the actual
usernames too, so restarting a shared host doesn't surprise someone else
using it.

## Working on the canvas

The live and design device palettes hide the infrastructure templates
`gns3-vm`, `ginnungagap-appliance` and every versioned appliance image
(`ginnungagap-appliance-…`, for example `ginnungagap-appliance-v1.0.0`). To replace that list, set
`TOPOLOGYOPERATOR_HIDDEN_TEMPLATE_NAMES` to comma-separated complete template
names. Matching is case-insensitive. The OVA catalog for host setup still
includes the GNS3 VM image.

Modelled on GNS3's own GUI. Everything below works in the offline design canvas;
the live canvas gets the same except where a change would touch real VMs.

- **Selection**: click a node; Ctrl/Cmd/Shift-click adds or removes one;
  Shift-drag on empty canvas draws a rubber band (Ctrl adds); Ctrl+A selects all.
  Several selected nodes open a selection popup (Start/Stop/Reload live, Copy in
  design mode, Delete) and move together when you drag one. The status bar shows
  running / capturing / selected counts.
- **Undo / redo** (Edit menu, Ctrl+Z / Ctrl+Y). In the design canvas every change
  is a step (up to 100, kept by the server in `app/design_history.py`, so it also
  covers delete, paste, reset, a generated topology and an opened project, and works
  during a template outage). In a live project only node moves and drawings are
  undoable (client-side) - adding or removing nodes/links changes real VMs, so those
  keep their confirmation dialogs instead.
- **Copy / paste / duplicate** (design only): copies nodes with the links between
  them and pastes them under new names (`R1` -> `R2`) as one undo step.
- **Delete** key deletes the selection (or the selected drawing).
- **Find node** (Ctrl+F): type part of a name, role or image, Enter centres the view
  on it and selects it.
- **Nodes menu** (live): Start All / Stop All / Reload All.
- **Link conditions** (live, link popup): delay + jitter, packet loss, corruption and
  suspend, applied to the running GNS3 link (its "filters"); an impaired link is drawn
  dashed orange, a suspended one dashed red. Filters set elsewhere (bpf,
  frequency drop) are kept, never wiped.
- **Keyboard**: arrows move the selection (Shift = 5x), + / - / 0 zoom and fit,
  `?` lists every shortcut (also Edit > Keyboard Shortcuts).

**Name it and deploy**: File > Deploy to Host asks for a project name when the design was never
saved - it saves the design to the library under that name and deploys it in one step (a different
name than the open project's saves a new project). Save Project also just asks for a name.

**Deploy from the warning banner**: when the project selected for the active host
does not exist in GNS3 yet ("No GNS3 project named ... has this topology actually been
deployed?"), the banner shows a **Deploy project...** button next to Retry. It asks
for confirmation (node/link counts and whether the host deploys incrementally or
rebuilds everything - a full deployment can remove devices that are not in the project)
and then runs the same job as File > Deploy to Host. `GET /health` reports it as
`deployable_project`.

**Start the GNS3 VM from the warning banner**: while the banner is shown and the configured GNS3 VM is switched off
(or suspended) on the ESXi host - typically after the host restarted, nothing powers it on by itself - the banner offers
**Start GNS3 VM**. It powers the VM on, then asks the server every few seconds (`POST /reload`) until VMware Tools report
an address (up to 5 minutes) and reloads the canvas. A VM that is on but reports no address gets **Restart GNS3 VM**
instead (a hard reset, behind a confirmation). `GET /health` reports the state as `gns3_vm` (`name`, `power_state`,
`action`: `start`, `restart`, `deploy` (VM missing entirely) or null; only asked while something is wrong, and never waits for an ESXi connect),
`POST /gns3-vm/start` does the action. A GNS3 VM that does not exist on the host at all (and a template for a new one is
selected) gets **Deploy GNS3 VM...** instead, which runs the File menu's deploy flow. The same buttons sit in the
template-service banner at the top of every screen (`/templates/health` carries `gns3_vm` while that service has a
problem), next to **Check again**: the template services read their sources only on a timer (GNS3 every 5 minutes, the
ESXi share every 15), so that button - and, once a started GNS3 VM has an address, the page by itself - calls
`POST /templates/refresh`, which asks both services' `POST /api/refresh` to run their collector right away (repeated
after a few seconds because the GNS3 service inside a fresh VM needs a moment longer than its address) and waits up to a
minute for them to list templates again. `/templates/health` reports `refreshable` (a service answered, so asking helps);
a service without that endpoint, or not set up for it (the host needs the request folder and the `.path` unit from
Docker-Server-Files), is simply left to its own timer - the answer says which one took the request.

## Template Share

The Admin Panel lists the ESXi templates and the status of their OVA files. Use **Import template…** to upload an OVA and
enter its catalog information. The Operator writes the file with its SHA-256 in the name and generates the YAML. Use
**Edit…** to update metadata. The catalog reflects changes on the next collector run.
An upload can contain one OVA up to 32 GiB.

Enable **Allow admins to manage ESXi templates in the Admin Panel** in the Appliance setup under **Template store**.
On a standalone or gateway installation, set `TOPOLOGYOPERATOR_TEMPLATE_SHARE_DIR` on the process serving the Admin Panel
to the absolute path of its already mounted writable share. An optional `TOPOLOGYOPERATOR_TEMPLATE_REFRESH_MINUTES`
sets the refresh interval shown in the panel. GNS3 templates are collected from GNS3.

## Device configs saved with the project

The Config Push tab sends raw command lists per device (JSON: device name -> list of lines, sent verbatim; **up to 10 000
commands per device**). Select a host before pushing commands or opening its ESXi serial console.
With **Save to project** the lists are stored with the library project (`configs.json` next to its
topology; **Load from project** brings them back, and they are loaded when the tab opens). The switch **Push automatically
after deploy and resume** makes the app push them by itself - right after the deploy of that project (they travel in the
deployment bundle) and after a **Resume** from hold. Endpoints: `GET/PUT /project/configs` (the active host's project) and
`GET/PUT /projects/{id}/configs`; limits: 200 devices, 10 000 commands per device, 4000 characters per command, 8 MB.

**Insert snippet…** appends a saved snippet to one device in the editor. It preserves other devices and options such as
`via` on the target. Review the combined commands before choosing **Push** or **Save to project**. Manage the library
through `GET /config-snippets` (list), `GET/PUT/DELETE /config-snippets/{name}` (read, create/update, delete).
For example, PUT `{"label":"Router info","commands":["show version"]}` to `/config-snippets/router-info.json`.
The library is shared across projects on this Operator instance. Files are stored in `config_snippets/` by default
(`TOPOLOGYOPERATOR_CONFIG_SNIPPETS_DIR`); keep credentials out of shared snippets.

**View → Config Push → Generate with AI** takes a device and a prompt. The Operator supplies the
active device's role, image, interfaces and adjacent links to the model. It supports VLAN creation
and Access VLAN assignment on GNS3 Cisco IOSvL2, IPv4 addresses on IOSv routed ports, and
physical port descriptions on both. The Operator renders commands from validated settings.
**Review and push…** confirms the target and commands before the normal Config Push job runs.
The push appears in History. A draft belongs to its author and target, can be used once, and
expires in ten minutes.

An administrator enables the feature in **Admin Panel → AI configuration**. It defaults to off.
The Operator stores the switch in `ai_config.json` and checks it again before pushing.
Set `TOPOLOGYOPERATOR_AI_CONFIG_FILE` to an absolute writable path for a service installation.
Both services need the same random `XEONIS_AI_SNIPPETS_TOKEN` (at least 32 printable ASCII
characters without spaces). Set `TOPOLOGYOPERATOR_AI_SNIPPET_GENERATOR_URL` in the Operator service
environment to pin the destination for the token, independent of host profiles. Use an internal
network or HTTPS. Update the Generator before enabling the switch. When the Operator's
variables are stored in its own `.env` file, start Uvicorn in the installation directory
with `--env-file .env`, or load the file from the service manager.

Set `XEONIS_CLASSROOM_MODE=1` in **both services** for a classroom installation to turn off the general
AI Health Check suggestions. The AI configuration switch applies to the whole Operator instance.
Before offering the shared lab to pupils, protect the Operator behind school authentication and enforce
role and class permissions for configuration writes, project access and pushes at the API. Give the
Generator an internal address and keep direct service access restricted to the Operator and administrators.

A push runs as a **background job** (`POST /config-push/jobs`, then `GET /config-push/jobs/{id}`): the tab shows a progress
bar per device and the per-device results at the end; only one push runs at a time. (`POST /config-push` still answers in
one request, for short lists.) A very long result is shortened to the failed commands plus the last 50 (`truncated`).

**History** shows the last 50 config pushes on this instance, newest first, including automatic pushes after deploy and
resume. It shows the outcome and per-device command counts without storing the commands, their output, or login details.
`GET /config-push/audit?limit=10` returns the latest ten runs. History persists in the separate `push_audit.jsonl` file
(`TOPOLOGYOPERATOR_PUSH_AUDIT_FILE`); the oldest entries are dropped once the cap is reached.

**The push solves what it can by itself.** A device that is still booting (nothing reachable yet, no command run) is retried
every 15 s for up to **15 minutes** - for pushes started by hand too (`"wait_seconds": 0` in the request: try once; up to 3600);
the job's status lists the devices it is waiting for (`waiting_for`; the tab shows "Waiting for: ..." under the progress). A Windows VM that went to sleep is woken with a key
press, a VM that ESXi suspended is resumed, and a device that raises something unforeseen ends only its own entry - the others
still run. When it still cannot get in it says why instead of dumping a fault: no VMware Tools / stuck before the OS starts
(look at the VM's screen), or a GNS3 VM whose CPU is at 95 % or higher (too few vCPUs - its router/switch images never finish booting).

**How a device is reached**
- GNS3 consoles (routers, VPCS): Telnet through Netmiko. No login is attempted - a console that asks for one fails.
- ESXi VMs with VMware Tools: guest operations, which log in by themselves with the account from the template catalog
  (the `root` entry, else the first). Guest operations only report an exit code, so each command's output is redirected
  into a temporary file in the guest, fetched through ESXi and deleted - the result shows the real output (failed commands
  expanded). Commands reach bash single-quoted, so quotes, `$variables` and pipes arrive unchanged. About one second per
  command. A **Windows VM that went to sleep** (powered on, but VMware Tools gone - Windows does that when idle) is woken by
  a key press on its virtual keyboard before the push, up to 20 s of waiting; a VM suspended by ESXi itself is not resumed.
- ESXi VMs **without** VMware Tools (MikroTik, pfSense, OPNsense, anything): the **serial console** (below). About 70 ms
  per command.

**Serial console agent.** `app/serial_agent.py` is a small expect engine that logs in on the VM's serial console and types
the commands like an operator: it wakes the console, answers the login / password / console-menu screens, and then runs
each command, recognising where its output ends - on shells by an end marker carrying the real exit status, on
RouterOS by the returning prompt plus the CLI's error texts. Profiles: `linux` (any shell), `routeros` (MikroTik; logs in as
`user+ct200w`), `bsd_menu` (pfSense/OPNsense: opens a shell through menu entry 8); the default is chosen by image name.
Windows is not supported. A device stops at its first failing command, the others continue.
- **Enable it per VM**: **automatic** during a config push when it is needed - for a device with `via: serial`, and for `auto` on a
  MikroTik/pfSense/OPNsense VM (appliances never run VMware Tools, serial is their only way in). A Linux/Windows VM whose Tools merely
  are not up yet is never power-cycled; use `via: serial` on it on purpose. Or by hand: click the VM, then **Enable serial console** (or `POST /nodes/{name}/serial/enable`,
  `GET /nodes/{name}/serial` for the state). That adds a network serial port (`tcp://:2000-2010` on the ESXi host, the
  firewall ruleset `remoteSerialPort` is switched on) and **powers the VM off and on again** - ESXi only allows the change
  while it is off. For a Linux guest with VMware Tools the app also starts `serial-getty@ttyS0` so there is a login prompt;
  appliances have to bring their own serial console: MikroTik has it out of the box (verified live); a stock OPNsense
  image stayed completely silent on its serial line - pfSense/OPNsense need the serial console switched on in their own
  settings first (the agent then reports "the serial console stayed completely silent"). The `bsd_menu` profile is covered by
  tests only (pfSense's OVA now imports, but it was not run through the agent yet).
- **Choosing the way in**: a device can be a plain command list (`"auto"`: guest operations while VMware Tools runs,
  otherwise the serial console if the VM has one) or an object
  `{"commands": [...], "via": "serial", "profile": "routeros", "username": "...", "password": "..."}` - `via` is `auto`,
  `guest_ops` or `serial`; the login defaults to the template catalog's account. (An explicit password in the object is
  stored in the project's `configs.json` on the server - prefer the catalog.)
- **Carrying on after an error**: by default a device stops at its first failing command. `"continue_on_error": true` in the
  object keeps running the remaining commands (useful for a ping without answer or a `grep` without hit); the device still
  counts as failed. A command that could not even be started, a lost connection and a failed `@reboot` always stop it.

**Restarting a device in the middle of a push.** Two lines in a device's command list are not sent but steer the push:
`@reboot [seconds]` restarts the device, waits until it is back and then carries on with the next commands;
`@wait-reboot [seconds]` is for a command that restarts the device by itself (promoting a domain controller does) - it waits
until the device went down and is back. `seconds` is the longest wait (default 900). Typical use: `Rename-Computer ...`,
`@reboot`, then the commands that need the new name. How "back" is recognised: on a VM with VMware Tools when guest
operations work again (Linux and Windows, verified live), on the serial console when the login prompt reappears (MikroTik
verified live; Linux profile; not pfSense/OPNsense, whose profile has no restart command). GNS3 devices reject the lines
before connecting (save with `write memory`, restart the node from the GUI). A device that does not go down, or is not back
in time, fails at that line and the rest of its list is not run. Any other line - also one that merely starts with `@`, like
`@echo off` - is an ordinary command.

Automatic pushes (after a deploy, and after a resume - as a job you can follow in the tab) wait for devices that are still
booting (`not_ready`: unreachable, nothing ran) and retry them every 15 s for up to 15 minutes; a device where a command
already ran is never repeated. Nothing in the app logs commands, output or passwords.

## One structural change at a time

Adding/removing a node or link edits the topology file and then deploys the whole graph; hold/resume and a
project import also rewrite the lab. They now queue behind one lock (`app/live_lock.py`; the log says "waiting
for another change to the lab to finish") instead of overlapping - three quick "Add node" clicks used to start
three deploys at once, one of which left a VM half imported. Start/stop, reads and the design canvas are not
affected.

Node names are unique (case-insensitive) even across requests still in flight: the server reserves a name
the moment an "Add node" request arrives - before it may have to wait for the lock - and refuses a second one
("just being deployed"); the GUI does the same check and never suggests a name that is being added. A failed
live add also puts the topology file back exactly as it was (TopologyBackend already writes the file only after a
successful deploy, so this is a safety net against a partly written file, not a fix for a known failure); what a failed
deploy half created on ESXi/GNS3 shows up under File > Find Orphans.

## Staging several live edits

Every add/remove above deploys immediately, one call at a time. The **Stage Changes** button (top bar, live mode
only) opens a draft copy of the live topology instead: add/remove nodes and links there first, see a plain-text
diff against what is actually live, then **Review & Apply** once to run every change back-to-back. It does not
reduce the number of underlying deploys (there is no multi-node batch API) or make them non-disruptive - it
removes the friction of confirming and watching each one separately. Applying stops at the first failing step;
everything before it has already happened live and stays applied, and staging stays open so the rest can be
reviewed again or discarded. Each apply requires the `review_id` from the most recent preview. If the draft
changed after review, open the preview again. If the live topology or selected project changed since staging
started, discard the stale draft and start a new one (`POST /live/stage`, `GET /live/stage/diff`, `POST /live/stage/apply`, see
`app/staging.py`/`app/batch_apply.py`). Deliberately narrow: only nodes and links, no drawings/notes/labels/undo -
those belong to the separate, much older design sketch (`app/design.py`).

## Deleting and holding projects

**Delete**: select a project in the project library dialog and choose **Delete project...**. Only the
library entry (topology and design) goes; whatever is deployed in GNS3/ESXi stays. The server refuses while
a host has the project selected or while it is on hold, and names the hosts.

**Hold** (File > Hold Project...) parks the live project of the active host instead of deleting it: its ESXi
VMs are powered off, its GNS3 project is closed (GNS3 stops every node when closing) and everything is kept.
A later deployment - even a full one - leaves the parked VMs alone: they are renamed `<name> [hold <id8>]`
and their annotation gets the prefix `topologyoperator-hold:<project id>`, which TopologyBuilder's cleanup
(it only touches annotations starting with `topologybuilder-image:`, and finds VMs by node name) never
matches. The record of what is parked lives in `holds.json` (gitignored; `TOPOLOGYOPERATOR_HOLDS_FILE`).
While a project is on hold the banner shows **Resume project** (and no Deploy button), the library marks it
"on hold", and deploying it is refused - **Resume** it instead: the app renames the VMs back, opens the GNS3
project and runs the same fix pass as the health check (starts nodes and VMs, recreates missing port
groups). To resume from the library/deploy dialog the project must be the host's selected one - the dialog
selects it and restarts the app for you. Limits: run one project at a time (two projects can use the same VLAN
ids on the shared trunk); a full deployment of another project resets the vSwitch, so a parked project's port
groups are recreated on resume; two projects may not share a GNS3 project name while one is on hold.
Endpoints: `POST /project/hold`, `POST /projects/{id}/resume`, `DELETE /projects/{id}`; `GET /health` reports
`held_project`, `GET /projects` flags `held`.

## Internet uplink

Add GNS3's built-in **NAT** node (palette entry "NAT (Internet uplink)", cloud icon, auto-named
`Internet`) and link it to a router or switch. It has one port (`nat0`, chosen automatically) and
gives everything behind it DHCP, DNS and NAT out through the GNS3 VM's own network - verified live:
a VPCS behind it got `192.168.42.x`, resolved names and pinged 8.8.8.8. Requirements: the GNS3 VM
itself must reach the internet, and an ESXi VM only gets it when it is linked (through GNS3 switches)
to the NAT node and runs a DHCP client. One uplink per topology is the sensible limit (all NAT nodes
share the same network); to the topology file it is a normal node (`image: NAT`, `role: FW` -
TopologyBuilder only knows five roles), deployed through its built-in-node path. The LLM generator
knows it too: ask for "internet access".

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
each library project therefore retains its own canvas layout. **Snapshots…** (File
menu, once a project is bound) checkpoints the current sketch under that project
without replacing it - unlike Save Project, nothing is overwritten, so earlier
points stay reachable; Restore replaces the current sketch with a snapshot's
(Undo brings the previous state back), Delete removes one for good. Capped at
50 per project, oldest dropped first (`app/project_snapshots.py`). **Clear Topology…** (File menu, design
mode only) empties the sketch after a confirmation; Edit > Undo brings it back. **Deploy to Host…**
shows the target ESXi address, GNS3 VM, node/link totals, role totals, and whether
the host uses incremental or full deployment. The preview is read-only. For a full
deployment, the preview closes before the final confirmation appears. Cancel
returns to the preview with the selected host and project. Starting
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

- `GET/POST /login`, `POST /logout`, `GET /me` - session login/logout and the
  signed-in username/role; see Accounts above. Everything else requires a
  session.
- `GET/POST /admin/users`, `PUT /admin/users/{username}/role`, `PUT
  /admin/users/{username}/password`, `PUT /admin/users/{username}/allowed-hosts`,
  `DELETE /admin/users/{username}`, `POST /admin/users/bulk` - admin-only user
  management. `allowed_hosts` (default `"ALL"`) restricts which
  settings-profile filenames a non-admin user may see/activate - admins are
  always unrestricted. Bulk-create takes `{"lines": "user;pass\n...", "role":
  ..., "allowed_hosts": ...}` (one account per line, split on the first `;`
  only so a password may itself contain `;` or any other character;
  best-effort - one bad line doesn't stop the rest).
- `GET/PUT /admin/ai-config` - admin-only AI configuration switch.
- `GET /admin/audit-log` - admin-only named security/admin events (see Accounts above).
- `POST /admin/logs/summarize` - admin-only AI summary of the last 2,000 log
  lines (see Accounts above); `GET /logs/download` remains open to every
  signed-in user, unchanged.
- `GET /projects`, `POST /projects` (name plus optional topology_yaml),
  `DELETE /projects/{id}`, `GET /projects/{id}/download`: managed project
  library, with no deployment.
- `POST /project/hold` (parks the active host's live project), `POST
  /projects/{id}/resume`; `GET/PUT /projects/{id}/configs` and `GET/PUT
  /project/configs`: per-device saved commands, for a library project and
  the active host's live one respectively.
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
- `GET /host-config/schema` (the form's field groups), `POST /host-config/preview`
  (renders the YAML without saving), `POST /host-config/test` and `POST
  /settings-profiles/{filename}/connections/test` (the same connection checks,
  for a not-yet-created host and an existing one respectively).
- Browsers navigating to an unknown address, a wrong method or a failing page get
  a readable 404/405/5xx page (`app/error_pages.py`); requests that don't ask for
  HTML (fetch, curl) keep getting the JSON `{"detail": ...}` body. There is no
  separate 403 page - nothing here is access-controlled.
- Design canvas editing: `GET /design/history`, `POST /design/undo|redo`,
  `POST /design/layout/batch` (group move), `POST /design/delete` (selection),
  `POST /design/paste`; live: `POST /layout/batch`, `PUT /links/{a}/{b}/options`
  (delay/jitter/loss/corruption/suspend; `GET /links` reports them back).
- `GET /design/templates`, `GET/POST /design/labels`, `GET /design/notes`,
  `PUT /design/notes/{name}`, `GET /design/nodes/{name}/credentials` - design
  mode's own copies of the equivalent live endpoints below.
- `GET /nodes/{name}/ports`, `GET/POST /labels`, `GET /notes`, `PUT
  /notes/{name}`, `GET /nodes/{name}/credentials`.
- `POST /design/project` `{name}`: saves the current design as a new library project (the deploy
  dialog names and saves it itself, then deploys it).
- `GET /design/project/{id}/snapshots`, `POST .../snapshots` `{name}`, `POST
  .../snapshots/{snapshot_id}/restore`, `DELETE .../snapshots/{snapshot_id}` - named
  checkpoints under a library project, see above.
- `DELETE /logs`: permanently clears the log (both rotation files); the first
  new line records that it happened.
- `GET /ova-templates`: safe OVA resource metadata proxied from the sister API;
  filenames, source directories, credentials, and server paths are omitted.

- `GET /health` (includes `warning` and returns `esxi_ip: null` until a host
  profile is active), `POST /reload` (same response shape, after retrying startup),
  `GET /topology` (static graph, 503 while `app.state.graph` is down),
  `GET /nodes` (graph merged with live GNS3/ESXi state), `GET/POST
  /layout`.
- `GET /templates`, `POST /nodes`, `DELETE /nodes/{name}`, `POST
  /links`, `DELETE /links/{node1}/{node2}[?if1=..&if2=..]` - live add/remove.
- `POST /nodes/{name}/start`, `.../stop`, `.../reload` - node lifecycle.
- `GET /nodes/{name}/serial`, `POST /nodes/{name}/serial/enable` - the ESXi
  VM serial port setup "Serial console agent" above needs; `WS
  /ws/serial/{name}` is the terminal connection itself, once enabled.
- `GET/POST /live/stage` (open/check a staging session), `POST /live/stage/discard`,
  `GET /live/stage/topology`, `POST /live/stage/nodes`, `DELETE /live/stage/nodes/{name}`,
  `POST /live/stage/links`, `DELETE /live/stage/links/{node1}/{node2}`, `GET /live/stage/diff`,
  `POST /live/stage/apply` `{"confirm": true}` - stage several live edits, see above.
- `GET /links` (every live link + capturing state), `POST
  /links/{node1}/{node2}/capture/start`, `.../capture/stop`, `GET
  .../capture/stream` (proxies GNS3's own live pcap stream) - see
  Architecture above.
- `POST /config-push` (one request, for short lists), `POST /config-push/jobs` (background job; body `devices` plus optional
  `wait_seconds`: how long devices that are still booting are retried - default 900, `0` = try once, max 3600) and
  `GET /config-push/jobs/{id}` (`status`, per-device progress, `waiting_for` = names of the devices the push is waiting for,
  `results` once done), `GET/POST /drawings`. A push configures **routers, switches and firewalls first** (GNS3 nodes and
  serial-console appliances such as MikroTik) and waits for them; the VMs (guest operations) follow only afterwards, because
  their commands and checks (e.g. a ping to the internet) need a network that carries traffic. Results keep the order asked for.
- `GET /logs?lines=N` (the in-project panel), `GET /logs/entries?lines=N&level=L&search=S` (entries with
  tracebacks kept together, plus how full the log is), `GET /logs/download` (the whole retained log as text),
  `GET /templates/health`, `POST /templates/refresh` - see Architecture above.
- `GET /config-snippets`, `GET/PUT/DELETE /config-snippets/{name}` - the saved-command
  snippet library (see Device configs above). `GET /config-snippets/ai/status`,
  `GET /config-snippets/ai/targets`, `POST /config-snippets/ai/generate`, `POST
  /config-snippets/ai/push` - the AI config-draft flow, admin-gated by `GET/PUT
  /admin/ai-config` above. `GET /config-push/audit?limit=N` - the last pushes'
  outcomes (see Device configs above).
- `GET /export/gns3project` streams the selected live GNS3 project's native archive.
- `GET /project/export`, `POST /project/import/preview` (dry-run: what a
  `.topoproj` import would remove), `POST /project/import` (kicks off a
  background job, returns `{job_id, total}`), `GET /project/import/{job_id}`
  (progress + final result), `POST /project/reload-gns3` (closes and
  reopens the live GNS3 project, restarting whichever nodes were
  running - refreshes GNS3's own state without redeploying anything).
- `GET/POST /settings-profiles`, `DELETE /settings-profiles/{filename}`,
  `POST /settings-profiles/{filename}/activate` (see Architecture above).
- `GET /design/topology`, `POST /design/nodes`, `DELETE
  /design/nodes/{name}`, `POST /design/links`, `DELETE
  /design/links/{node1}/{node2}`, `GET/POST /design/layout`, `GET/POST
  /design/drawings`, `POST /design/reset`, `GET /design/export`, `POST
  /design/generate`, `POST /design/generate/attachments`, `GET
  /design/generate/{job_id}` - design mode, all mirroring the equivalent
  live endpoint above (see Architecture above).
- `GET /design/project` (the design canvas's own project-library list, as
  opposed to `POST /design/project/{id}/open` above which opens one of them).
- `POST /ai/suggest-fix` (body: `device`, `check`, optional `goal`; returns `job_id`),
  `GET /ai/suggest-fix/{job_id}` (status, attempts and the draft or error). The generator request uses
  `device_role`, `device_image`, `context` and `goal`; the reply uses `valid`, `commands`, `explanation` and `warnings`.
- `POST /maintenance/verify` (`{"fix": bool}`; returns the repairs applied
  and every check's result), `GET /maintenance/orphans`, `POST
  /maintenance/orphans/delete` (`{"orphans": [{"type": "gns3_node" |
  "esxi_vm", "name": ...}]}`, permanent) - see Architecture above.
- `POST /maintenance/connectivity-test`, `GET
  /maintenance/connectivity-test/{job_id}` (see Architecture above).
- `POST /gns3-vm/deploy`, `GET /gns3-vm/deploy/{job_id}`, `POST /gns3-vm/start`
  (the warning banner's Start/Restart GNS3 VM button) - see GNS3 VM Deploy above.
- `WS /ws/console/{node}`, `WS /ws/vm-screen/{node}`, `WS /ws/serial/{node}`.
- `GET /license` - the repo's LICENSE file as plain text, for the landing page's
  own "View License" link.

The frontend at `/` covers all of these.

## Application version

The version (`MAJOR.MINOR.PATCH`, currently `1.0.0`) lives in exactly one place: the single line `__version__ = "1.0.0"` in
`app/version.py`. It is a plain file under `app/` on purpose: a deploy by raw file copy (as on the lab testbox) carries it along,
whereas git and the installed package metadata can be missing or stale there. `pyproject.toml` reads the same value
(`dynamic = ["version"]`), so a built package cannot disagree; a test fails if the two ever drift. To bump it, edit that one line.

It is reported as `version` by `GET /health` (backend) and `GET /gateway/health` (gateway), shown as `v1.0.0` in the
landing page footer next to "View License" / "View Logs", and in the footer of the gateway's choose-a-server page. It is
plain text - no hover effect or tooltip.

## Testing

```bash
pip install -e ".[test]"
pytest
```

1065 tests, all offline (mocked ESXi/pyvmomi, real local TCP servers
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
