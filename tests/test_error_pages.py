import pytest
from fastapi.testclient import TestClient

from app import main

HTML = {"accept": "text/html,application/xhtml+xml"}


def test_browser_gets_a_404_page(client):
    response = client.get("/no-such-page.html", headers=HTML)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("text/html")
    assert "Page not found" in response.text
    assert 'href="/"' in response.text


def test_api_client_still_gets_json(client):
    response = client.get("/no-such-page.html")
    assert response.status_code == 404
    assert response.json() == {"detail": "Not Found"}


def test_wrong_method_is_a_405_page_in_a_browser(client):
    response = client.post("/health", headers=HTML)
    assert response.status_code == 405
    assert "Not available this way" in response.text


def test_reason_is_escaped_and_paths_redacted(client):
    response = client.get("/settings-profiles/<b>x</b>.yml/connections", headers=HTML)
    assert "<b>x</b>" not in response.text


def test_unexpected_error_is_a_generic_page_without_details():
    @main.app.get("/_boom_for_test")
    def boom():
        raise RuntimeError("secret internal detail /srv/app/x.py")

    # Registered after the catch-all static mount, so move it in front of it.
    route = main.app.router.routes.pop()
    main.app.router.routes.insert(0, route)

    try:
        # No "with": that would run the real lifespan (connects to ESXi).
        c = TestClient(main.app, raise_server_exceptions=False)
        page = c.get("/_boom_for_test", headers=HTML)
        api = c.get("/_boom_for_test")
    finally:
        main.app.router.routes.remove(route)
    assert page.status_code == 500
    assert "Something went wrong" in page.text
    assert "secret internal detail" not in page.text
    assert api.json() == {"detail": "Internal Server Error"}


@pytest.mark.parametrize("status", [403, 404, 405, 500, 502])
def test_render_has_a_title_for_each_status(status):
    from app import error_pages

    assert f"<title>{status} - " in error_pages.render(status).body.decode()
