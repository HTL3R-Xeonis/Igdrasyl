# TopologyOperator

A GUI tool for *operating* topologies already deployed by
[TopologyBuilder](https://github.com/HTL3R-Xeonis/TopologyBuilder): a
GNS3-like canvas, in-window access to each device's console/screen, and
automated per-device configuration push from a second config file. A
separate tool from TopologyBuilder itself, depending on it as a library
rather than duplicating its ESXi/GNS3 connection code.

**Status: backend + frontend built and verified for GNS3 console
embedding and config-push; VM screens blocked on a real ESXi-build bug.**
See `HANDOFF.md` for the full history and design rationale if picking
this up fresh.

## Architecture

- Web app: a FastAPI backend (`app/`) + a plain HTML/JS frontend
  (`app/static/index.html`), not a native desktop app.
- Depends on TopologyBuilder as an installed library
  (`ESXiConnection`/`GNS3Connection`/`Settings`/`Graph`), not a copy of
  its code. Its top-level package name is `src`, so this project's own
  code lives under `app/` specifically to avoid a namespace collision.
- GNS3 console embedding: `xterm.js` in the browser, bridged to the
  node's real Telnet console port via a backend WebSocket proxy
  (`app/console.py`).
- VM screens: **not WebMKS** (see below) - a direct VNC server enabled
  on the VM itself (`RemoteDisplay.vnc.*`), bridged the same way
  (`app/vm_screen.py`), rendered in the browser with open-source `noVNC`.
- Config push: a second YAML file (raw CLI commands per device, not a
  structured schema - see `HANDOFF.md` for the rationale), pushed via
  Netmiko (GNS3 console devices) or `pyvmomi` guest-ops (ESXi VMs), the
  same transports the interactive console/screen features use. Stops on
  the first failed command per device, but continues to the next device
  (`app/config_push.py`).

## Running it

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
cp settings.example.yml settings.yml   # point topology_file at a real deployed topology
cp .env.example .env                    # fill in ESXI_PASSWORD/GNS3_PASSWORD
export TOPOLOGYOPERATOR_SETTINGS=settings.yml
uvicorn app.main:app --reload
```

Endpoints: `GET /health`, `GET /topology` (static graph), `GET /nodes`
(graph merged with live GNS3/ESXi state), `WS /ws/console/{node}`,
`WS /ws/vm-screen/{node}` + `POST /vm-screen/{node}/prepare`,
`POST /config-push`. The frontend at `/` covers all of these.

## Testing

```bash
pip install -e ".[test]"
pytest
```

31 tests, all offline (mocked ESXi/pyvmomi, real local TCP servers standing
in for GNS3 consoles/Netmiko targets - no real infrastructure needed).
Covers `app/topology.py`, `app/gns3_state.py`, `app/console.py`,
`app/config_push.py`, `app/vm_screen.py`, and the `main.py` HTTP endpoints.
Everything here has also been separately verified against real
infrastructure at least once (see `HANDOFF.md` for exactly what and when) -
these tests are the permanent, repeatable form of that verification, not
a replacement for it.

## Current state

- `app/` - the FastAPI backend + frontend. `/health`, `/topology`,
  `/nodes` (Phase 0/1, includes live GNS3 status *and* ESXi VM power
  state), GNS3 console embedding (`console.py`), config-push
  (`config_push.py`), VM screens via direct VNC (`vm_screen.py` - code is
  correct and independently verified, but blocked end-to-end by a
  real ESXi-build bug, see `HANDOFF.md`).
- `tests/` - the pytest suite described above.
- `spikes/webmks/` - the original WebMKS proof-of-concept. Superseded by
  the direct-VNC approach in `app/vm_screen.py` for the real app, but
  kept as-is; both hit their own separate bugs on this project's real
  ESXi host (see `HANDOFF.md`), likely the same root cause (a 6+ year
  unpatched build).
