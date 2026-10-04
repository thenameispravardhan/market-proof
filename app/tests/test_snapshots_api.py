"""Chart snapshot store: PNG in, short id out, served back; junk refused."""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.api import snapshots

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def test_snapshot_round_trip(client: TestClient, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(snapshots, "SNAP_DIR", tmp_path / "snaps")
    r = client.post("/api/snapshots", content=PNG, headers={"Content-Type": "image/png"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True and body["url"] == f"/api/snapshots/{body['id']}.png"
    got = client.get(body["url"])
    assert got.status_code == 200
    assert got.headers["content-type"] == "image/png"
    assert got.content == PNG


def test_snapshot_rejects_non_png_and_bad_ids(client: TestClient, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(snapshots, "SNAP_DIR", tmp_path / "snaps")
    assert client.post("/api/snapshots", content=b"GIF89a....", headers={"Content-Type": "image/png"}).status_code == 422
    # a traversal attempt never yields an image (the path normalises away from this route)
    assert client.get("/api/snapshots/..%2F..%2Fetc.png").headers.get("content-type") != "image/png"
    assert client.get("/api/snapshots/a.b.png").status_code == 404  # malformed id
    assert client.get("/api/snapshots/nope1234.png").status_code == 404


def test_snapshot_store_is_pruned(client: TestClient, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(snapshots, "SNAP_DIR", tmp_path / "snaps")
    monkeypatch.setattr(snapshots, "_MAX_FILES", 3)
    for _ in range(5):
        client.post("/api/snapshots", content=PNG, headers={"Content-Type": "image/png"})
    assert len(list((tmp_path / "snaps").glob("*.png"))) == 3
