"""
Readable error pages for people who navigate to a URL in the browser.

Only a request that asks for HTML (a browser address bar or link - fetch()
from app.js sends Accept: */* and keeps getting JSON) is answered with a
page; every API client keeps the JSON {"detail": ...} body. 404 is the one
that actually happens (mistyped address, an old bookmark, /logs.htm);
405 and 5xx get the same page so nothing ever shows a bare JSON blob or
a stack-trace-less "Internal Server Error" to a person. There is no 403
page of its own because nothing here is access-controlled - a 403 coming
from the lab (e.g. GNS3 refusing something) is reported inside the app, and
would render through this same page if it ever reached a navigation.
"""

from __future__ import annotations

from html import escape

from fastapi import Request, Response

_TITLES = {
    403: ("Not allowed", "You don't have access to this."),
    404: ("Page not found", "There is nothing at this address."),
    405: (
        "Not available this way",
        "This address doesn't accept that kind of request.",
    ),
}
_SERVER_ERROR = (
    "Something went wrong",
    "The server hit an unexpected problem. The details are in the log.",
)


def wants_html(request: Request) -> bool:
    return "text/html" in request.headers.get("accept", "")


# Layer states shown on the "not reachable" card: "ok" (reached), "fail" (the layer that is down), "unknown" (not
# checked because a lower layer failed). Same four layers the in-page overlay (connection-monitor.js) shows.
LAYER_STATES = ("ok", "fail", "unknown")


def unavailable_html(host: str | None, backend_state: str) -> str:
    """
    The page a browser gets when the gateway cannot reach the selected host's server (a navigation or a reload during an
    outage): same look as the in-page "Connection lost" overlay, no exception text, refreshes itself every 10 seconds.
    `backend_state` is "starting", "stopped" or "unreachable".
    """
    name = escape(host) if host else "the selected host"
    title = (
        f"The server for {name} is starting"
        if backend_state == "starting"
        else f"The server for {name} is not reachable"
    )
    message = (
        "It was started a moment ago and is not answering yet. This page tries again by itself."
        if backend_state == "starting"
        else "The gateway is running, but the server of this host does not answer. This page tries again by itself."
    )
    layers = (
        ("Browser", "ok"),
        ("Gateway", "ok"),
        ("Server", "fail"),
        ("Host", "unknown"),
    )
    words = {"ok": "reached", "fail": "not reachable", "unknown": "not checked"}
    items = "".join(
        f'<li class="cl-layer cl-{state}"><span class="cl-dot"></span>{escape(label)}'
        f'<span class="cl-state">{words[state]}</span></li>'
        for label, state in layers
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>Connection lost - Ginnungagap</title>
<link rel="icon" type="image/svg+xml" href="/ginnungagap-mark.svg">
<link rel="stylesheet" href="/styles.css?v=ginnungagap-63">
<style>
  body {{ display: block; height: auto; min-height: 100vh; overflow: auto; }}
  .cl-page {{ min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; }}
</style>
</head>
<body>
<div class="cl-page">
  <main class="cl-card" role="alert">
    <img class="cl-mark" src="/ginnungagap-mark.svg" alt="">
    <h1 class="cl-title">{title}</h1>
    <p class="cl-message">{escape(message)}</p>
    <ul class="cl-layers">{items}</ul>
    <nav class="cl-actions">
      <a class="toolbar-btn" href="">Retry now</a>
      <a class="toolbar-btn" href="/pick-backend">Choose another server</a>
    </nav>
  </main>
</div>
</body>
</html>"""


def render_unavailable(host: str | None, backend_state: str) -> Response:
    return Response(
        unavailable_html(host, backend_state),
        status_code=502,
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


def render(status_code: int, detail: str | None = None) -> Response:
    title, message = _TITLES.get(status_code) or (
        _SERVER_ERROR
        if status_code >= 500
        else ("Request failed", "The request failed.")
    )
    shown = ""
    # Only the server's own short reason for a 4xx; for a 5xx the detail may be
    # an internal message, so the page stays generic and the log has the rest.
    if detail and status_code < 500 and detail not in ("Not Found", "Forbidden"):
        shown = f"<p class='detail'>{escape(detail)}</p>"
    body = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{status_code} - {escape(title)}</title>
<link rel="icon" type="image/svg+xml" href="/ginnungagap-mark.svg">
<link rel="stylesheet" href="/styles.css?v=ginnungagap-63">
<style>
  /* styles.css lays <body> out as the single-page app - this is its own page. */
  body {{ display: block; height: auto; min-height: 100vh; overflow: auto; }}
  main {{ display: block; text-align: center; padding: 18vh 24px 24px; max-width: 460px; margin: 0 auto; }}
  .code {{ font-size: 64px; font-weight: 300; color: var(--dim); line-height: 1; }}
  h1 {{ font-size: 20px; font-weight: 500; margin: 12px 0 8px; }}
  p {{ color: var(--dim); margin: 6px 0; }}
  .detail {{ color: var(--text); }}
  nav {{ margin-top: 22px; display: flex; gap: 10px; justify-content: center; }}
  nav a {{ text-decoration: none; }}
</style>
</head>
<body>
<main>
  <div class="code">{status_code}</div>
  <h1>{escape(title)}</h1>
  <p>{escape(message)}</p>
  {shown}
  <nav>
    <a class="toolbar-btn" href="/">Back to Ginnungagap</a>
    <a class="toolbar-btn" href="/logs.html">View Logs</a>
  </nav>
</main>
</body>
</html>"""
    return Response(content=body, status_code=status_code, media_type="text/html")
