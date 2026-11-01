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
  screen (`GET /templates/health`); "Image ... not found" errors get the
  likely cause appended. When the service cannot be reached at all, the
  startup banner and the 503 answers of `/templates`, `/design/templates`,
  `/design/nodes` and `/topology` name the service and the reason ("The ESXi
  template service (host) is not reachable: connection refused or host
  down.") instead of a raw connection error; design mode keeps working.
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
  **retries by itself** in the background (every 15 s at first, then every
  minute, never while another change to the lab runs or a project is on hold)
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

## GNS3 VM Deploy

Replaces the configured GNS3 VM with a freshly imported one from its own
OVA (`src.gns3_vm_deploy.deploy_fresh_gns3_vm`, in TopologyBuilder) -
available two ways. The new VM always gets **4 vCPUs and 8 GB RAM** (set after the
import, before it is powered on), whatever the OVA declares - the stock OVA's
1 vCPU / 2 GB cannot boot a handful of IOSv nodes:

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
profile. The whole form is one scrollable page: General, ESXi host, Networking, GNS3, API services and
Credentials cover 22 host/runtime settings plus both passwords, with
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

## Working on the canvas

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

## Device configs saved with the project

The Config Push tab sends raw command lists per device (JSON: device name -> list of lines, sent verbatim; **up to 10 000
commands per device**). With **Save to project** the lists are stored with the library project (`configs.json` next to its
topology; **Load from project** brings them back, and they are loaded when the tab opens). The switch **Push automatically
after deploy and resume** makes the app push them by itself - right after the deploy of that project (they travel in the
deployment bundle) and after a **Resume** from hold. Endpoints: `GET/PUT /project/configs` (the active host's project) and
`GET/PUT /projects/{id}/configs`; limits: 200 devices, 10 000 commands per device, 4000 characters per command, 8 MB.

A push runs as a **background job** (`POST /config-push/jobs`, then `GET /config-push/jobs/{id}`): the tab shows a progress
bar per device and the per-device results at the end; only one push runs at a time. (`POST /config-push` still answers in
one request, for short lists.) A very long result is shortened to the failed commands plus the last 50 (`truncated`).

**The push solves what it can by itself.** A device that is still booting (nothing reachable yet, no command run) is retried
every 15 s for up to **15 minutes** - for pushes started by hand too (`"wait_seconds": 0` in the request: try once; up to 3600);
the job's status lists the devices it is waiting for (`waiting_for`; the tab shows "Waiting for: ..." under the progress). A Windows VM that went to sleep is woken with a key
press, a VM that ESXi suspended is resumed, and a device that raises something unforeseen ends only its own entry - the others
still run. When it still cannot get in it says why instead of dumping a fault: no VMware Tools / stuck before the OS starts
(look at the VM's screen), or a GNS3 VM whose CPU is at 100 % (too few vCPUs - its router/switch images never finish booting).

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
- Design canvas editing: `GET /design/history`, `POST /design/undo|redo`,
  `POST /design/layout/batch` (group move), `POST /design/delete` (selection),
  `POST /design/paste`; live: `POST /layout/batch`, `PUT /links/{a}/{b}/options`
  (delay/jitter/loss/corruption/suspend; `GET /links` reports them back).
- `POST /design/project` `{name}`: saves the current design as a new library project (the deploy
  dialog names and saves it itself, then deploys it).
- `DELETE /logs`: permanently clears the log (both rotation files); the first
  new line records that it happened.
- `GET /ova-templates`: safe OVA resource metadata proxied from the sister API;
  filenames, source directories, credentials, and server paths are omitted.

- `GET /health` (includes `warning` - see the graceful-degrade point
  above), `POST /reload` (manual retry once a down dependency recovers),
  `GET /topology` (static graph, 503 while `app.state.graph` is down),
  `GET /nodes` (graph merged with live GNS3/ESXi state), `GET/POST
  /layout`.
- `GET /templates`, `POST /nodes`, `DELETE /nodes/{name}`, `POST
  /links`, `DELETE /links/{node1}/{node2}[?if1=..&if2=..]` - live add/remove.
- `POST /nodes/{name}/start`, `.../stop`, `.../reload` - node lifecycle.
- `GET /links` (every live link + capturing state), `POST
  /links/{node1}/{node2}/capture/start`, `.../capture/stop`, `GET
  .../capture/stream` (proxies GNS3's own live pcap stream) - see
  Architecture above.
- `POST /config-push` (one request, for short lists), `POST /config-push/jobs` (background job; body `devices` plus optional
  `wait_seconds`: how long devices that are still booting are retried - default 900, `0` = try once, max 3600) and
  `GET /config-push/jobs/{id}` (`status`, per-device progress, `waiting_for` = names of the devices the push is waiting for,
  `results` once done), `GET/POST /drawings`.
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

504 tests, all offline (mocked ESXi/pyvmomi, real local TCP servers
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
