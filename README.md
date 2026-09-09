# TopologyOperator

A GUI tool for *operating* topologies already deployed by
[TopologyBuilder](https://github.com/HTL3R-Xeonis/TopologyBuilder): a
GNS3-like canvas, in-window access to each device's console/screen, and
automated per-device configuration push from a second config file. A
separate tool from TopologyBuilder itself, depending on it as a library
rather than duplicating its ESXi/GNS3 connection code.

**Status: Phase 0/1 FastAPI backend skeleton exists (read-only), plus
the WebMKS spike below.** See `HANDOFF.md` for the full history and
design rationale if picking this up fresh.

## Planned architecture

- Web app (FastAPI backend + plain frontend - `xterm.js` for GNS3
  console, embedded VM screens via WebMKS tickets rendered with
  open-source `noVNC`, not VMware's proprietary SDK - see
  `spikes/webmks/README.md`), not a native desktop app - forced by
  WebMKS being fundamentally browser technology.
- Depends on TopologyBuilder as an installed library
  (`ESXiConnection`/`GNS3Connection`/`Settings`/`Graph`), not a copy of
  its code.
- Config push: a second YAML file (raw CLI commands per device, not a
  structured schema - see rationale in the design memory), pushed via
  Netmiko (GNS3 console devices) or `pyvmomi` guest-ops (ESXi VMs), the
  same transports the interactive console/exec features use. Stops on
  the first failed command per device, but continues to the next device.

## Current state

- `app/` - the real FastAPI backend, Phase 0/1: read-only only so far.
  Depends on TopologyBuilder as a library (see `pyproject.toml`, pinned
  to its `dev` branch), reusing its `Settings`/`Graph`/`ESXiConnection`/
  `GNS3Connection` wholesale rather than duplicating any connection
  code. Copy `settings.example.yml` to `settings.yml` and
  `.env.example` to `.env`, point `TOPOLOGYOPERATOR_SETTINGS` at the
  former, then `uvicorn app.main:app --reload`. Endpoints so far:
  `/health`, `/topology` (the topology file's own structure, no live
  state), `/nodes` (topology nodes merged with live GNS3 status/console
  port+type - not yet tested against real infrastructure, see
  `HANDOFF.md`). No frontend, no GNS3 console proxy, no config-push
  engine, no VM guest-ops yet.
- `spikes/webmks/` - a standalone, throwaway proof of concept testing
  whether WebMKS console embedding actually works against this project's
  real ESXi infrastructure. Currently blocked (see `HANDOFF.md`) -
  parked, not gating the rest of the app. See `spikes/webmks/README.md`.
