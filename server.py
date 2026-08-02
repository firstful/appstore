#!/usr/bin/env python3
"""Sutliff Apps store backend — catalog + APK hosting + OTA manifests.

Endpoints:
  GET  /health
  GET  /apps.json                 catalog: [{package, name, version, apk, size, built}]
  GET  /version/<package>.json    OTA manifest {app, version, apk}
  GET  /apk/<StoreDir>/<file>     APK download
  POST /publish/<StoreDir>/<file> CI publish (multipart file + manifest form field,
                                  auth: X-Publish-Token == APPSTORE_PUBLISH_TOKEN)
Storage: /data (mounted PVC; NFS bulk — APKs are re-creatable artifacts).
"""
import json
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse

DATA = Path(os.environ.get("STORE_DATA", "/data"))
DATA.mkdir(parents=True, exist_ok=True)
TOKEN = os.environ.get("APPSTORE_PUBLISH_TOKEN", "")

app = FastAPI(title="Sutliff Apps Store")


def manifest_path(pkg: str) -> Path:
    return DATA / "version" / f"{pkg}.json"


def catalog() -> list[dict]:
    out = []
    vdir = DATA / "version"
    if vdir.exists():
        for mf in sorted(vdir.glob("*.json")):
            try:
                m = json.loads(mf.read_text())
                apk_path = DATA / m["apk"]
                m["exists"] = apk_path.exists()
                out.append(m)
            except Exception:
                continue
    return out


@app.get("/health")
def health():
    return {"status": "ok", "store": "sutliff-apps"}


@app.get("/apps.json")
def apps():
    return JSONResponse(catalog())


@app.get("/version/{pkg}.json")
def version(pkg: str):
    p = manifest_path(pkg)
    if not p.exists():
        raise HTTPException(404, "no such app")
    return JSONResponse(json.loads(p.read_text()))


@app.get("/apk/{store_dir}/{file}")
def apk(store_dir: str, file: str):
    p = (DATA / "apk" / store_dir / file).resolve()
    if not str(p).startswith(str(DATA)) or not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(p, media_type="application/vnd.android.package-archive",
                        filename=p.name)


@app.post("/publish/{store_dir}/{file}")
async def publish(store_dir: str, file: str,
                  x_publish_token: str = Header(default=""),
                  payload: UploadFile = File(...),
                  manifest: str = Form(...)):
    if not TOKEN or not secrets.compare_digest(x_publish_token, TOKEN):
        raise HTTPException(401, "bad publish token")
    try:
        m = json.loads(manifest)
    except Exception:
        raise HTTPException(400, "manifest must be JSON")
    pkg = m.get("app", "")
    if not pkg:
        raise HTTPException(400, "manifest missing app")
    dest = DATA / "apk" / store_dir
    dest.mkdir(parents=True, exist_ok=True)
    # overwrite-in-place: keep a .prev of the last build for rollback
    target = dest / file
    if target.exists():
        shutil.copy2(target, dest / f"{file}.prev")
    with target.open("wb") as fh:
        shutil.copyfileobj(payload.file, fh)
    m.update({
        "apk": f"apk/{store_dir}/{file}",
        "size": target.stat().st_size,
        "built": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })
    vdir = DATA / "version"
    vdir.mkdir(exist_ok=True)
    (vdir / f"{pkg}.json").write_text(json.dumps(m, indent=2))
    return {"ok": True, "package": pkg, "apk": m["apk"], "size": m["size"]}
