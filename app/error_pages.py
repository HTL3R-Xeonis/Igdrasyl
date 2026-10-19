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
<link rel="stylesheet" href="/styles.css?v=uplink-3">
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
    <a class="toolbar-btn" href="/">Back to TopologyOperator</a>
    <a class="toolbar-btn" href="/logs.html">View Logs</a>
  </nav>
</main>
</body>
</html>"""
    return Response(content=body, status_code=status_code, media_type="text/html")
