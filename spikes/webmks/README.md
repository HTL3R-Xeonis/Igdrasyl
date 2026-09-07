# WebMKS spike

Standalone test of whether WebMKS console embedding works against this
project's real ESXi infrastructure, before building any GUI around it.
Nothing here depends on TopologyOperator's own (not-yet-built) backend -
it's a throwaway two-file proof of concept: a Python script that acquires
a console ticket, and an HTML page that renders the console using it.

## Why this needs testing before anything else

The planned GUI embeds each VM's actual screen in-window using VMware's
WebMKS protocol (`AcquireTicket(ticketType="webmks")` + the WMKS
JavaScript SDK). This is the least-proven, highest-risk piece of the
whole design - unlike everything else planned (GNS3 console access, VM
guest-command execution), nobody has confirmed this actually renders
correctly against this specific infrastructure yet.

Two things were already checked by research rather than assumption
(2026-09-07):
- The *old* `AcquireMksTicket()` API is broken on ESXi 8
  ([vmware/pyvmomi#1078](https://github.com/vmware/pyvmomi/issues/1078)) -
  irrelevant here, this spike uses the newer `AcquireTicket(ticketType=
  "webmks")` call, which is unaffected.
- `AcquireTicket("webmks")` works directly against a standalone ESXi host
  (no vCenter needed) - confirmed via multiple independent sources.
- **Known gotcha already handled in the script**: the ticket's own
  `.host` field can come back wrong/unreachable on a standalone host -
  the script builds the WebSocket URL using the host you actually
  connected to instead of trusting that field.

What's still unverified and exactly what this spike is for: whether the
WMKS JavaScript client can actually open that WebSocket and render a
real screen against *this* ESXi host's TLS setup, ESXi version, and
network path - versus failing on a cert/handshake/version mismatch that
only shows up by actually trying it.

## One manual step - the WMKS SDK isn't included here

`wmks.min.js`/`wmks-all.css` are a proprietary Broadcom/VMware SDK
distributed as a ZIP from their own site, not published to any public
CDN or npm - so it can't be fetched automatically. Download the "VMware
HTML Console SDK" (aka WebMKS SDK) from Broadcom's site (free, may need a
support-portal login), unzip it, and copy `wmks.min.js` and
`wmks-all.css` into this directory, next to `webmks_test.html`. The
image assets that ship alongside those files in the SDK zip should come
along too if `wmks-all.css` references any by relative path.

## Running it

```bash
pip install -r requirements.txt
python webmks_spike.py --host 10.20.20.202 --user root --password '<esxi password>' --vm <any already-running VM name>
```

It doesn't need to be a TopologyBuilder-deployed VM - any powered-on VM
on the host works, this only tests whether WebMKS itself functions here.

Copy the printed `wss://...` URL, open `webmks_test.html` in a browser,
paste the URL into the input field, and click Connect.

## What to look for

- **Success**: the VM's actual screen renders in the black `#console`
  box, and you can click/type into it.
- **Failure modes to report back verbatim** (paste the browser console's
  error, and the "State: ..." text under the Connect button):
  - A TLS/certificate error in the browser console - likely needs the
    self-signed ESXi cert trusted by the browser first (visit
    `https://10.20.20.202/` directly once and accept the warning, since
    WebSocket connections don't get their own "proceed anyway" prompt).
  - `CONNECTION_STATE_CHANGE` reporting an error state immediately -
    usually an auth/permission issue (the connecting user needs
    `VirtualMachine.Interact.ConsoleInteract` privilege - `root` should
    already have this).
  - The socket connects but the screen never draws - possible WMKS
    SDK/ESXi version mismatch, worth noting the exact WMKS SDK version
    downloaded and the ESXi version (`vmware -v` on the host, or visible
    in the host's own web UI).

Whatever happens, paste the result back - this assistant has no network
access to `10.20.20.0/24` and can't run this spike itself.
