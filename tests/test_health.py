"""The health check, the common error format and the local UI."""


def test_healthz(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json() == {"status": "ok"}


def test_every_response_has_a_request_id(client):
    assert client.get("/healthz").headers.get("X-Request-ID")


def test_request_id_from_client_is_kept(client):
    assert client.get("/healthz", headers={"X-Request-ID": "abc123"}).headers["X-Request-ID"] == "abc123"


def test_unknown_route_uses_standard_error_format(client):
    res = client.get("/nope")
    assert res.status_code == 404
    assert res.json()["error"]["code"] == "not_found"


def test_docs_and_ui_hidden_outside_local(client):
    # The test settings use env="test": developer pages must not be served.
    assert client.get("/docs").status_code == 404
    assert client.get("/ui/").status_code == 404


def test_ui_served_locally(deps):
    from fastapi.testclient import TestClient

    from app.main import create_app

    deps.settings.env = "local"
    local = TestClient(create_app(deps))
    assert local.get("/", follow_redirects=False).headers["location"] == "/ui/"
    page = local.get("/ui/")
    assert page.status_code == 200 and "SLM Extraction Bench" in page.text
    assert local.get("/ui/vendor/pdf.min.mjs").headers["content-type"].startswith("text/javascript")
