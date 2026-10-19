"""Settings → Server restart: needs the typed confirm; never actually stops the test process."""


def test_restart_needs_the_typed_confirm(client):
    assert client.post("/api/system/restart", json={"what": "app"}).status_code == 422
    r = client.post("/api/system/restart", json={"what": "reboot", "confirm": "RESTART"}).json()
    assert r == {"ok": True, "what": "reboot", "dry_run": True}
