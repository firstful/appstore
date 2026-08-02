#!/usr/bin/env python3
"""Sutliff Apps store backend — catalog + APK hosting + OTA manifests + web UI.

Endpoints:
  GET  /                          web frontend: app catalog + APK download links
  GET  /health
  GET  /apps.json                 catalog: [{package, name, version, apk, size, built}]
  GET  /version/<package>.json    OTA manifest {app, version, apk}
  GET  /apk/<StoreDir>/<file>     APK download
  POST /publish/<StoreDir>/<file> CI publish (multipart file + manifest form field,
                                  auth: X-Publish-Token == APPSTORE_PUBLISH_TOKEN)
Storage: /data (mounted PVC; NFS bulk — APKs are re-creatable artifacts).
"""
import html
import json
import os
import secrets
import shutil
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

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


@app.get("/", response_class=HTMLResponse)
def index():
    """Basic install page: render the catalog with APK download buttons."""
    entries = catalog()
    cards = []
    for m in entries:
        pkg = m.get("app", "?")
        apk_rel = m.get("apk", "")
        ver = m.get("version", "?")
        size = m.get("size", 0)
        built = m.get("built", "")
        exists = m.get("exists", False)
        try:
            size_mb = f"{size / 1048576:.1f} MB"
        except Exception:
            size_mb = "?"
        try:
            built_d = datetime.fromisoformat(built).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            built_d = built or "?"
        # human name: last package segment, title-cased (e.g. com.x.talaria -> Talaria)
        name = pkg.rsplit(".", 1)[-1].title() if "." in pkg else pkg
        if exists:
            btn = (f'<a class="btn" href="/apk/{html.escape(apk_rel, quote=True)}" '
                   f'download>Install APK</a>')
        else:
            btn = '<span class="btn off">APK missing</span>'
        cards.append(f"""<div class="card">
  <div class="card-top"><span class="name">{html.escape(name)}</span>
  <span class="ver">v{html.escape(str(ver))}</span></div>
  <div class="meta"><code>{html.escape(pkg)}</code></div>
  <div class="meta">{size_mb} &middot; {html.escape(built_d)}</div>
  <div class="actions">{btn}
  <a class="link" href="/version/{html.escape(pkg)}.json">manifest</a></div>
</div>""")
    body = "\n".join(cards) if cards else (
        '<p class="empty">Store is empty &mdash; nothing published yet.</p>')
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sutliff Apps</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 640px;
         margin: 0 auto; padding: 24px 16px; }}
  h1 {{ font-size: 1.4rem; margin: 8px 0 4px; }}
  p.sub {{ opacity: .7; margin: 0 0 20px; font-size: .9rem; }}
  .card {{ border: 1px solid #8884; border-radius: 12px; padding: 14px 16px;
          margin-bottom: 12px; }}
  .card-top {{ display: flex; justify-content: space-between; align-items: baseline; }}
  .name {{ font-weight: 600; font-size: 1.05rem; }}
  .ver {{ opacity: .65; font-size: .85rem; }}
  .meta {{ opacity: .6; font-size: .85rem; margin-top: 2px; }}
  code {{ font-size: .8rem; }}
  .actions {{ margin-top: 10px; display: flex; align-items: center; gap: 14px; }}
  .btn {{ background: #2f81f7; color: #fff; text-decoration: none; padding: 8px 18px;
         border-radius: 8px; font-weight: 600; font-size: .92rem; }}
  .btn.off {{ background: #8886; cursor: not-allowed; }}
  .link {{ font-size: .85rem; opacity: .7; }}
  .empty {{ opacity: .6; }}
</style></head>
<body>
<h1>Sutliff Apps</h1>
<p class="sub">Family app store &mdash; install APKs straight to your phone.
Raw catalog: <a href="/apps.json">/apps.json</a></p>
{body}
</body></html>"""


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
