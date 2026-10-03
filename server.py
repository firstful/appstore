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
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import zipfile
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


def apk_facts(path: Path) -> dict:
    """sha256 of the stored APK. Always computed here, never trusted from CI.

    Cheap, exact, and it gives the client a second independent signal: if the
    hash is unchanged the bytes are identical, so there is nothing to install
    even when the metadata is ambiguous.
    """
    try:
        return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    except OSError:
        return {}


def apk_version_code(path: Path) -> int | None:
    """versionCode from the APK, when this container can read one.

    Returns None in normal operation: the appstore image deliberately has no
    Android SDK (it is a small python image, not a build image), so aapt2 is
    never present. The value that ships in the catalog comes from CI, which
    does have aapt2 and reads it from the artifact it just built.

    This remains as a best-effort enrichment for anyone running the store
    locally on a machine that happens to have the SDK. It is never the
    authority — see publish(), which prefers the APK but accepts CI's value.
    """
    sdk = os.environ.get("ANDROID_HOME", "")
    try:
        for cand in sorted(Path(sdk).glob("build-tools/*/aapt2"), reverse=True):
            if not cand.exists():
                continue
            try:
                badging = subprocess.run(
                    [str(cand), "dump", "badging", str(path)],
                    capture_output=True, text=True, timeout=120, check=True,
                ).stdout
            except Exception:
                continue
            m = re.search(r"versionCode='(\d+)'", badging)
            if m:
                return int(m.group(1))
    except OSError:
        pass
    return None


def catalog() -> list[dict]:
    out = []
    vdir = DATA / "version"
    if vdir.exists():
        for mf in sorted(vdir.glob("*.json")):
            try:
                m = json.loads(mf.read_text())
                apk_path = DATA / m["apk"]
                m["exists"] = apk_path.exists()
                # Backfill on read: apps published before this change have no
                # versionCode/sha256 in their manifest, and every one of them
                # would otherwise show a permanently greyed-out button.
                # sha256 is computed from the stored bytes here; versionCode
                # can only be recovered with an SDK, so for already-published
                # apps it stays absent until their next CI publish.
                if m["exists"] and not m.get("sha256"):
                    m.update(apk_facts(apk_path))
                if m["exists"] and not m.get("versionCode"):
                    vc = apk_version_code(apk_path)
                    if vc is not None:
                        m["versionCode"] = vc
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
        # human name: prefer manifest name, else last package segment
        # (e.g. com.x.talaria -> Talaria)
        name = m.get("name") or (pkg.rsplit(".", 1)[-1].title() if "." in pkg else pkg)
        if exists:
            btn = (f'<a class="btn" href="/{html.escape(apk_rel, quote=True)}" '
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
    # sha256 is always computed here — it is cheap and exact.
    m.update(apk_facts(target))
    # versionCode: prefer reading the APK, but the appstore image has no
    # Android SDK, so that usually yields nothing. CI sends the real value
    # (it has aapt2); accept it. Order matters: if both are available and
    # disagree, the APK wins and we log it.
    vc = apk_version_code(target)
    if vc is None:
        sent = m.get("versionCode")
        vc = int(sent) if sent is not None else None
    elif m.get("versionCode") is not None and int(m["versionCode"]) != vc:
        print(f"warn: {pkg} manifest versionCode {m['versionCode']} != APK {vc}; using APK")
    if vc is not None:
        m["versionCode"] = vc
    vdir = DATA / "version"
    vdir.mkdir(exist_ok=True)
    (vdir / f"{pkg}.json").write_text(json.dumps(m, indent=2))
    return {
        "ok": True,
        "package": pkg,
        "apk": m["apk"],
        "size": m["size"],
        "versionCode": m.get("versionCode"),
        "sha256": m.get("sha256"),
    }
