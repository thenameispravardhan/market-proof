"""Chart snapshots — the Trade page's "Copy link" / "Tweet image".

POST /api/snapshots takes a PNG (the chart image the browser rendered) and
returns a short id; GET /api/snapshots/{id}.png serves it back. Files live
under data/snapshots and the oldest are pruned past _MAX_FILES, so the
folder can't grow without bound. Everything stays behind the site's basic
auth like the rest of the API — a link opens for anyone signed in to this
terminal, it is not a public image host.
"""
from __future__ import annotations

import re
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.logging_config import get_logger

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])
log = get_logger(__name__)

ROOT = Path(__file__).resolve().parents[2]
SNAP_DIR = ROOT / "data" / "snapshots"
_MAX_BYTES = 4_000_000
_MAX_FILES = 300
_PNG = b"\x89PNG\r\n\x1a\n"
_ID = re.compile(r"^[A-Za-z0-9_-]{6,32}$")


def _prune(folder: Path) -> None:
    files = sorted(folder.glob("*.png"), key=lambda p: p.stat().st_mtime)
    for p in files[: max(0, len(files) - _MAX_FILES)]:
        try:
            p.unlink()
        except OSError:
            pass


@router.post("")
async def create_snapshot(request: Request) -> dict[str, Any]:
    body = await request.body()
    if len(body) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="snapshot too large")
    if not body.startswith(_PNG):
        raise HTTPException(status_code=422, detail="expected a PNG image")
    folder = SNAP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    sid = secrets.token_urlsafe(9)
    (folder / f"{sid}.png").write_bytes(body)
    _prune(folder)
    log.info("snapshots.saved", id=sid, bytes=len(body))
    return {"ok": True, "id": sid, "url": f"/api/snapshots/{sid}.png"}


@router.get("/{sid}.png")
def get_snapshot(sid: str) -> FileResponse:
    if not _ID.match(sid):
        raise HTTPException(status_code=404, detail="not found")
    path = SNAP_DIR / f"{sid}.png"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})
