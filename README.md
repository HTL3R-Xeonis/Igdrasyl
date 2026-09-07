# TopologyOperator

A GUI tool for *operating* topologies already deployed by
[TopologyBuilder](https://github.com/HTL3R-Xeonis/TopologyBuilder): a
GNS3-like canvas, in-window access to each device's console/screen, and
automated per-device configuration push from a second config file. A
separate tool from TopologyBuilder itself, depending on it as a library
rather than duplicating its ESXi/GNS3 connection code.

**Status: design/scoping only, nothing built yet beyond the spike
below.** See the full architecture design in this project's memory
(`project_operator_tool_design.md` in TopologyBuilder's session memory)
if picking this up fresh.

## Planned architecture

- Web app (FastAPI backend + plain frontend - `xterm.js` for GNS3
  console, VMware's WebMKS for embedded VM screens), not a native
  desktop app - forced by WebMKS being fundamentally browser technology.
- Depends on TopologyBuilder as an installed library
  (`ESXiConnection`/`GNS3Connection`/`Settings`/`Graph`), not a copy of
  its code.
- Config push: a second YAML file (raw CLI commands per device, not a
  structured schema - see rationale in the design memory), pushed via
  Netmiko (GNS3 console devices) or `pyvmomi` guest-ops (ESXi VMs), the
  same transports the interactive console/exec features use. Stops on
  the first failed command per device, but continues to the next device.

## Current state

- `spikes/webmks/` - a standalone, throwaway proof of concept testing
  whether WebMKS console embedding actually works against this project's
  real ESXi infrastructure. This is the least-proven, highest-risk piece
  of the design, so it's being tested in isolation before any of the
  actual application (FastAPI backend, frontend, GNS3 console proxy,
  guest-exec, config push) gets built. See `spikes/webmks/README.md`.
