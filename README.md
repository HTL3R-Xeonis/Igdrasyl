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
config-push, project import/export, and multi-host switching have all
been exercised live, not just in the test suite.

## Architecture

- Web app: a FastAPI backend (`app/`) + a plain HTML/JS frontend
  (`app/static/index.html`), not a native desktop app.
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

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp settings.example.yml settings.yml   # point topology_file at a real deployed topology
cp .env.example .env                    # fill in ESXI_PASSWORD/GNS3_PASSWORD
export TOPOLOGYOPERATOR_SETTINGS=settings.yml
uvicorn app.main:app --reload
```

Open `http://localhost:8000/` (or wherever it's bound) - the landing
page lists whatever settings.yml profiles have been uploaded via the
UI's own "Add host" tile; the file `TOPOLOGYOPERATOR_SETTINGS` points
at on startup shows up there too, already marked active, so a first run
has something to click into immediately.

## Endpoints

- `GET /health`, `GET /topology` (static graph), `GET /nodes` (graph
  merged with live GNS3/ESXi state), `GET/POST /layout`.
- `GET /templates`, `POST /nodes`, `DELETE /nodes/{name}`, `POST
  /links`, `DELETE /links/{node1}/{node2}` - live add/remove.
- `POST /nodes/{name}/start`, `.../stop`, `.../reload` - node lifecycle.
- `POST /config-push`, `GET/POST /drawings`.
- `GET /project/export`, `POST /project/import` (kicks off a background
  job, returns `{job_id, total}`), `GET /project/import/{job_id}`
  (progress + final result), `POST /project/reload-gns3` (closes and
  reopens the live GNS3 project, restarting whichever nodes were
  running - refreshes GNS3's own state without redeploying anything).
- `GET/POST /settings-profiles`, `DELETE /settings-profiles/{filename}`,
  `POST /settings-profiles/{filename}/activate` (see Architecture above).
- `WS /ws/console/{node}`, `WS /ws/vm-screen/{node}`.

The frontend at `/` covers all of these.

## Testing

```bash
pip install -e ".[test]"
pytest
```

150 tests, all offline (mocked ESXi/pyvmomi, real local TCP servers
standing in for GNS3 consoles/Netmiko targets - no real infrastructure
needed).

## License

GNU GPLv3 - see [LICENSE](LICENSE).
