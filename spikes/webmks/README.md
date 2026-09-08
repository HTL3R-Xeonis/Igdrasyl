# WebMKS spike

Standalone test of whether WebMKS console embedding works against this
project's real ESXi infrastructure, before building any GUI around it.
Nothing here depends on TopologyOperator's own (not-yet-built) backend -
it's a throwaway proof of concept: a Python script that acquires a
console ticket, and an HTML page that renders the console using it.

## Why this needs testing before anything else

The planned GUI embeds each VM's actual screen in-window. This is the
least-proven, highest-risk piece of the whole design - unlike everything
else planned (GNS3 console access, VM guest-command execution), nobody
has confirmed this actually renders correctly against this specific
infrastructure yet.

## Use `novnc_test.html` - no download required

**This is the recommended path, not `webmks_test.html`.** On vSphere/ESXi
6+, WebMKS's actual wire protocol is standard RFB-over-WebSocket, not
anything VMware-proprietary - confirmed via
[`rgerganov/mks`](https://github.com/rgerganov/mks)'s own source
(`mks.py`'s `vsphere6_url` function connects a stock, unmodified noVNC
straight to the same `wss://<host>:<port>/ticket/<ticket>` URL this
project's own `webmks_spike.py` already prints - a modified noVNC/proxy
is only needed for legacy vSphere 5.x, not this infrastructure).
`novnc_test.html` loads noVNC's `rfb.js` straight from a CDN
(`cdn.jsdelivr.net/gh/novnc/noVNC@v1.7.0/...`), so there's nothing to
download or install for this test - or later, for the real tool, since
the shipped app can do the exact same CDN load (or vendor that one
MIT-licensed file if an offline build is ever needed - no license
friction, unlike VMware's SDK).

`webmks_test.html` (needs a manually-downloaded proprietary Broadcom SDK
- see below) is kept only as a fallback in case noVNC hits some
compatibility snag on this specific ESXi version - try `novnc_test.html`
first.

## Running it

```bash
pip install -r requirements.txt
python webmks_spike.py --host 10.20.20.202 --user root --password '<esxi password>' --vm <any already-running VM name>
```

It doesn't need to be a TopologyBuilder-deployed VM - any powered-on VM
on the host works, this only tests whether WebMKS itself functions here.

Copy the printed `wss://...` URL, open `novnc_test.html` in a browser,
paste the URL into the input field, and click Connect.

## What to look for

- **Success**: the VM's actual screen renders in the black `#screen` box,
  and you can click/type into it.
- **Failure modes to report back verbatim** (paste the browser console's
  error, and the status text under the Connect button):
  - A TLS/certificate error - likely needs the self-signed ESXi cert
    trusted by the browser first (visit `https://10.20.20.202/` directly
    once and accept the warning, since WebSocket connections don't get
    their own "proceed anyway" prompt).
  - `securityfailure` or an immediate disconnect - usually an
    auth/permission issue (the connecting user needs
    `VirtualMachine.Interact.ConsoleInteract` privilege - `root` should
    already have this).
  - The socket connects but the screen never draws - worth trying
    `webmks_test.html` (see below) to check whether it's a noVNC-specific
    quirk on this ESXi version rather than a WebMKS problem in general.

## Fallback: `webmks_test.html` (needs a manual download)

`wmks.min.js`/`wmks-all.css` are a proprietary Broadcom/VMware SDK
distributed as a ZIP from their own site, not published to any public
CDN or npm - so it can't be fetched automatically. Download the "VMware
HTML Console SDK" (aka WebMKS SDK) from Broadcom's site (free, may need a
support-portal login), unzip it, and copy `wmks.min.js` and
`wmks-all.css` into this directory, next to `webmks_test.html`. The
image assets that ship alongside those files in the SDK zip should come
along too if `wmks-all.css` references any by relative path. Same
ticket URL from `webmks_spike.py` works for either page.

Whatever happens, paste the result back - this assistant has no network
access to `10.20.20.0/24` and can't run this spike itself.
