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
    """Single-purpose landing page: get the Sutliff Apps client APK.

    This page used to render the whole catalog as a list of install buttons,
    which competed with the app it was advertising — the phone has a real
    store client that talks to /apps.json, and that client is what decides
    install-vs-update per app. A second, static list here can only ever be a
    stale copy of the catalog, so it was the wrong place to surface apps.

    What belongs here is exactly one thing: the download for the store client
    itself. Everything else stays reachable via /apps.json and /apk/..., which
    are the machine-facing endpoints and are unchanged.
    """
    store_dir = os.environ.get("APPSTORE_SELF_DIR", "SutliffApps")
    self_pkg = os.environ.get("APPSTORE_SELF_PKG", "com.reedsutliff.sutliffapps")
    m = {}
    try:
        m = json.loads(manifest_path(self_pkg).read_text())
    except Exception:
        pass

    apk_rel = m.get("apk", "")
    ver = m.get("version", "?")
    size = m.get("size", 0)
    exists = bool(apk_rel) and (DATA / apk_rel).exists()
    try:
        built_d = datetime.fromisoformat(m.get("built", "")).strftime("%d %b %Y")
    except Exception:
        built_d = ""
    try:
        size_mb = f"{size / 1048576:.1f} MB"
    except Exception:
        size_mb = "?"

    name = m.get("name") or "Sutliff Apps"
    if exists:
        btn = (f'<a class="btn" href="/{html.escape(apk_rel, quote=True)}" '
               f'download>Download APK</a>')
        meta = f"{size_mb} · v{html.escape(str(ver))}"
        if built_d:
            meta += f" · {built_d}"
    else:
        btn = '<span class="btn off">Not published yet</span>'
        meta = "The store client has not been published to this catalog."

    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Sutliff Apps</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: system-ui, -apple-system, sans-serif; max-width: 480px;
         margin: 0 auto; padding: 32px 16px; text-align: center; }}
  .icon {{ width: 96px; height: 96px; border-radius: 22px; margin: 0 auto 16px;
           background: #1c222a; display: flex; align-items: center;
           justify-content: center; }}
  .icon svg {{ width: 56px; height: 56px; }}
  h1 {{ font-size: 1.5rem; margin: 0 0 4px; }}
  p.sub {{ opacity: .7; margin: 0 0 24px; font-size: .92rem; line-height: 1.5; }}
  .card {{ border: 1px solid #8884; border-radius: 14px; padding: 18px 16px; }}
  .meta {{ opacity: .6; font-size: .85rem; margin-bottom: 14px; }}
  .btn {{ background: #4caf50; color: #fff; text-decoration: none; padding: 12px 26px;
         border-radius: 10px; font-weight: 600; font-size: .98rem;
         display: inline-block; }}
  .btn.off {{ background: #8886; cursor: not-allowed; }}
  p.foot {{ opacity: .5; font-size: .8rem; margin-top: 28px; }}
  code {{ font-size: .78rem; }}
</style></head>
<body>
  <div class="icon">
    <svg viewBox="0 0 96 96" fill="none" aria-hidden="true">
      <path d="M30 24 L48 46 L66 24" stroke="#4caf50" stroke-width="7"
            stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M26 58 V70 H70 V58" stroke="#4caf50" stroke-width="7"
            stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
  </div>
  <h1>{html.escape(name)}</h1>
  <p class="sub">The store client for this household. It reads the live catalog and
  installs or updates your apps &mdash; including Talaria.</p>
  <div class="card">
    <div class="meta">{meta}</div>
    {btn}
  </div>
  <p class="foot">Other endpoints: <a href="/apps.json">catalog</a> ·
  <a href="/health">health</a></p>
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
