"""FastAPI server for the Arabic subtitle builder."""
from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import time

import threading

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import core, jobs

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATE_DIR = os.path.join(BASE_DIR, "state")
EXAMPLES = os.path.join(BASE_DIR, "examples")
OUTPUT = os.path.join(BASE_DIR, "output")

os.makedirs(STATE_DIR, exist_ok=True)
os.makedirs(EXAMPLES, exist_ok=True)
os.makedirs(OUTPUT, exist_ok=True)

store = jobs.Store(os.path.join(STATE_DIR, "state.json"))
worker = jobs.Worker(store, BASE_DIR)
app = FastAPI(title="Arabic Subs Builder")


@app.on_event("startup")
def resume_pending_jobs():
    """After a restart, pick the queue back up instead of waiting for a click."""
    with store.lock:
        pending = any(j["status"] in ("queued", "running") for j in store.data["jobs"])
    if pending:
        worker.ensure_running()

_library: dict = {"root": None, "items": [], "scanned_at": None}


# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #

def allowed_roots() -> list[str]:
    roots = [EXAMPLES, OUTPUT]
    extra = store.data.get("settings", {}).get("roots", [])
    roots.extend(extra)
    return [os.path.realpath(r) for r in roots if r]


def safe_path(path: str, *, must_exist: bool = True) -> str:
    real = os.path.realpath(os.path.expanduser(path))
    if must_exist and not os.path.exists(real):
        raise HTTPException(404, f"not found: {path}")
    roots = allowed_roots()
    if not any(real == r or real.startswith(r + os.sep) for r in roots):
        raise HTTPException(403, "path outside allowed folders (add it in settings)")
    return real


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #

class ScanIn(BaseModel):
    root: str
    recursive: bool = True


class JobIn(BaseModel):
    paths: list[str]
    options: dict = {}


class SettingsIn(BaseModel):
    settings: dict


class SubIn(BaseModel):
    path: str
    text: str


@app.get("/api/health")
def health():
    checks = {"ffmpeg": bool(shutil.which("ffmpeg")), "ffprobe": bool(shutil.which("ffprobe"))}
    try:
        import faster_whisper  # noqa: F401
        checks["faster_whisper"] = True
    except Exception:
        checks["faster_whisper"] = False
    try:
        import transformers  # noqa: F401
        checks["transformers"] = True
    except Exception:
        checks["transformers"] = False
    models_dir = os.path.join(BASE_DIR, "models")
    checks["models_on_disk"] = sorted(
        d for d in os.listdir(models_dir) if os.path.isdir(os.path.join(models_dir, d))
    ) if os.path.isdir(models_dir) else []
    return checks


@app.get("/api/settings")
def get_settings():
    return {"settings": store.data["settings"], "roots": allowed_roots()}


@app.post("/api/settings")
def set_settings(body: SettingsIn):
    with store.lock:
        store.data["settings"].update(body.settings)
    store.save()
    return {"settings": store.data["settings"]}


@app.post("/api/scan")
def scan(body: ScanIn):
    root = safe_path(body.root)
    if os.path.isfile(root):
        root = os.path.dirname(root)
    items = core.scan_videos(root, body.recursive)
    _library.update({"root": root, "items": items, "scanned_at": time.time()})
    return {"root": root, "count": len(items),
            "hours": round(sum(i["duration"] for i in items) / 3600, 2), "items": items}


class FilesIn(BaseModel):
    paths: list[str]


@app.post("/api/add_files")
def add_files(body: FilesIn):
    """Load exactly these videos — no folder scan.

    Used by the picker when the user ticks individual files: scanning the
    whole folder just to select a handful is slow on large libraries.
    """
    if not body.paths:
        raise HTTPException(400, "no files given")
    items = []
    for p in body.paths:
        real = safe_path(p)
        if not os.path.isfile(real):
            continue
        items.extend(core.scan_videos(real))
    root = os.path.dirname(items[0]["path"]) if items else None
    _library.update({"root": root, "items": items, "scanned_at": time.time()})
    return {"root": root, "count": len(items),
            "hours": round(sum(i["duration"] for i in items) / 3600, 2), "items": items}


@app.get("/api/library")
def library():
    return _library


@app.post("/api/add_root")
def add_root(body: ScanIn):
    real = os.path.realpath(os.path.expanduser(body.root))
    if not os.path.isdir(real):
        raise HTTPException(400, "not a directory")
    with store.lock:
        roots = store.data["settings"].setdefault("roots", [])
        if real not in roots:
            roots.append(real)
    store.save()
    return {"roots": allowed_roots()}


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #

@app.post("/api/jobs")
def create_job(body: JobIn):
    paths = [safe_path(p) for p in body.paths]
    if not paths:
        raise HTTPException(400, "no files selected")
    job = jobs.new_job(store, paths, body.options)
    if job is None:
        return JSONResponse({"error": "those files are already queued or running"},
                            status_code=409)
    worker.ensure_running()
    return {"job": job}


@app.get("/api/live")
def live(path: str | None = None):
    """Transcript of the file being processed right now."""
    return worker.live_view(path)


@app.get("/api/jobs")
def list_jobs():
    with store.lock:
        return {"jobs": store.data["jobs"]}


@app.get("/api/jobs/{jid}")
def get_job(jid: str):
    j = store.job(jid)
    if not j:
        raise HTTPException(404, "no such job")
    return {"job": j}


@app.post("/api/jobs/{jid}/{action}")
def job_action(jid: str, action: str):
    j = store.job(jid)
    if not j:
        raise HTTPException(404, "no such job")
    if action == "pause":
        with store.lock:
            if j["status"] == "running":
                j["status"] = "paused"
        worker.pause_all()
    elif action == "resume":
        with store.lock:
            j["status"] = "queued"
            for it in j["items"]:
                if it["status"] == "running":
                    it["status"] = "pending"
    elif action == "cancel":
        with store.lock:
            j["status"] = "cancelled"
            for it in j["items"]:
                if it["status"] in ("pending", "running"):
                    it["status"] = "cancelled"
    elif action == "retry":
        with store.lock:
            for it in j["items"]:
                if it["status"] == "error":
                    it["status"] = "pending"
                    it["error"] = None
            j["status"] = "queued"
    else:
        raise HTTPException(400, f"unknown action {action}")
    store.save()
    if action == "cancel":
        worker.pause.clear()
    if action in ("resume", "retry"):
        worker.resume_all()
    return {"job": j}


@app.post("/api/jobs/purge")
def purge_jobs():
    """Drop finished/cancelled jobs from the queue. A running job is kept."""
    with store.lock:
        before = len(store.data["jobs"])
        store.data["jobs"] = [j for j in store.data["jobs"] if j["status"] == "running"]
        removed = before - len(store.data["jobs"])
    store.save()
    return {"removed": removed, "kept": len(store.data["jobs"])}


@app.delete("/api/jobs/{jid}")
def delete_job(jid: str):
    with store.lock:
        store.data["jobs"] = [j for j in store.data["jobs"] if j["id"] != jid]
    store.save()
    return {"ok": True}


@app.post("/api/jobs/pause-all")
def pause_all():
    worker.pause_all()
    return {"ok": True}


@app.post("/api/jobs/resume-all")
def resume_all():
    worker.resume_all()
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Subtitles
# --------------------------------------------------------------------------- #

def _subtitle_for(video: str, hint: str | None = None) -> str | None:
    """Find the subtitle for a video.

    The writer falls back to output/ when the video's folder is read-only,
    so look beside the video first, then in that mirror.
    """
    if hint:
        try:
            real = safe_path(hint)
            if os.path.exists(real):
                return real
        except HTTPException:
            pass

    stem = os.path.splitext(video)[0]
    for cand in (stem + ".ar.srt", stem + ".srt", stem + ".ar.ass", stem + ".ass"):
        if os.path.exists(cand):
            return cand

    base = os.path.basename(stem)
    for suffix in (".ar", ""):
        for ext in (".srt", ".ass"):
            cand = os.path.join(OUTPUT, base + suffix + ext)
            if os.path.exists(cand):
                return cand
    return None


@app.get("/api/subtitle")
def get_subtitle(path: str, fmt: str = "srt", sub: str | None = None):
    video = safe_path(path)
    sub = _subtitle_for(video, sub)
    if not sub:
        raise HTTPException(404, "no subtitle yet")
    with open(sub, encoding="utf-8-sig", errors="replace") as f:
        text = f.read()
    if fmt == "vtt":
        cues = parse_srt(text)
        return HTMLResponse(core.write_vtt(cues), media_type="text/vtt")
    return JSONResponse({"path": sub, "text": text})


def parse_srt(text: str) -> list[core.Cue]:
    cues = []
    blocks = text.replace("\r\n", "\n").split("\n\n")
    for b in blocks:
        lines = [l for l in b.strip().split("\n") if l.strip()]
        if len(lines) < 2:
            continue
        idx = 0
        if lines[0].strip().isdigit():
            idx = 1
        if idx >= len(lines) or "-->" not in lines[idx]:
            continue
        a, _, b2 = lines[idx].partition("-->")
        try:
            cues.append(core.Cue(_parse_t(a.strip()), _parse_t(b2.strip()),
                                 "\n".join(lines[idx + 1:])))
        except ValueError:
            continue
    return cues


def _parse_t(s: str) -> float:
    s = s.replace(",", ".")
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


@app.put("/api/subtitle")
def put_subtitle(body: SubIn):
    p = safe_path(body.path, must_exist=False)
    # keep whatever BOM the file already had — TVs depend on it
    had_bom = False
    if os.path.exists(p):
        with open(p, "rb") as f:
            had_bom = f.read(3) == b"\xef\xbb\xbf"
    with open(p, "w", encoding="utf-8-sig" if had_bom else "utf-8") as f:
        f.write(body.text)
    return {"ok": True, "path": p, "bom": had_bom}


@app.get("/api/media")
def media(path: str, request: Request):
    """Serve a video for the in-browser preview player (with range support)."""
    p = safe_path(path)
    return FileResponse(p, headers={"Accept-Ranges": "bytes"})


@app.get("/api/download")
def download(path: str):
    p = safe_path(path)
    return FileResponse(p, filename=os.path.basename(p))



# --------------------------------------------------------------------------- #
# Folder browser
# --------------------------------------------------------------------------- #

BROWSE_ROOTS = [os.path.realpath(os.path.expanduser("~")), "/run/media", "/media", "/mnt", BASE_DIR]


def _in_browse_scope(path: str) -> bool:
    return any(path == r or path.startswith(r + os.sep) for r in BROWSE_ROOTS)


@app.get("/api/browse")
def browse(path: str | None = None):
    base = os.path.realpath(os.path.expanduser(path or BROWSE_ROOTS[0]))
    if not _in_browse_scope(base):
        raise HTTPException(403, "outside the browsable area")
    if not os.path.isdir(base):
        raise HTTPException(404, "not a directory")
    try:
        names = sorted(os.listdir(base), key=str.lower)
    except PermissionError:
        raise HTTPException(403, "permission denied")

    entries = []
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(base, name)
        try:
            if os.path.isdir(full):
                entries.append({"name": name, "path": full, "type": "dir"})
            elif os.path.splitext(name)[1].lower() in core.VIDEO_EXTS:
                entries.append({"name": name, "path": full, "type": "video",
                                "size": os.path.getsize(full)})
        except OSError:
            continue

    parent = os.path.dirname(base)
    return {
        "path": base,
        "parent": parent if (parent != base and _in_browse_scope(parent)) else None,
        "home": BROWSE_ROOTS[0],
        "roots": BROWSE_ROOTS,
        "entries": entries,
    }


# --------------------------------------------------------------------------- #
# Model management
# --------------------------------------------------------------------------- #

MODEL_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large-v3": "Systran/faster-whisper-large-v3",
}
MODEL_MB = {"tiny": 75, "base": 145, "small": 484, "medium": 1500, "large-v3": 3100}

_dl_lock = threading.Lock()
_dl = {"size": None, "status": "idle", "pct": 0, "error": None, "mb": 0}


def _model_dirs() -> dict[str, list[str]]:
    root = os.path.join(BASE_DIR, "models")
    out = {}
    for size, repo in MODEL_REPOS.items():
        d = "models--" + repo.replace("/", "--")
        out[size] = [os.path.join(root, d), os.path.join(root, "hf", "hub", d)]
    return out


def _dir_mb(path: str) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1e6


@app.get("/api/models")
def models_status():
    dirs = _model_dirs()
    cached, mb = [], {}
    for size, paths in dirs.items():
        got = max((_dir_mb(p) for p in paths if os.path.isdir(p)), default=0)
        mb[size] = round(got)
        if got > MODEL_MB[size] * 0.6:
            cached.append(size)

    with _dl_lock:
        dl = dict(_dl)
    if dl["status"] == "downloading" and dl["size"]:
        paths = dirs.get(dl["size"], [])
        got = max((_dir_mb(p) for p in paths if os.path.isdir(p)), default=0)
        dl["mb"] = round(got)
        dl["pct"] = min(99, round(got / MODEL_MB[dl["size"]] * 100))
    return {"cached": cached, "expected_mb": MODEL_MB, "on_disk_mb": mb, "download": dl}


class ModelIn(BaseModel):
    size: str


@app.post("/api/models/download")
def download_model(body: ModelIn):
    if body.size not in MODEL_REPOS:
        raise HTTPException(400, "unknown model")
    with _dl_lock:
        if _dl["status"] == "downloading":
            return {"download": dict(_dl)}
        _dl.update({"size": body.size, "status": "downloading", "pct": 0, "error": None, "mb": 0})

    def run():
        try:
            from faster_whisper.utils import download_model as fetch

            fetch(body.size, cache_dir=os.path.join(BASE_DIR, "models"), local_files_only=False)
            with _dl_lock:
                _dl.update({"status": "done", "pct": 100})
        except Exception as exc:  # noqa: BLE001
            with _dl_lock:
                _dl.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})

    threading.Thread(target=run, daemon=True, name="model-download").start()
    with _dl_lock:
        return {"download": dict(_dl)}


# --------------------------------------------------------------------------- #
# Server-sent events
# --------------------------------------------------------------------------- #

@app.get("/api/events")
def events():
    def gen():
        last = None
        while True:
            with store.lock:
                snapshot = json.dumps({
                    "jobs": [{k: j.get(k) for k in
                              ("id", "status", "current", "audio_done", "eta", "options")}
                             | {"items": [{k: it.get(k) for k in
                                           ("path", "name", "status", "progress", "out", "error", "elapsed", "lang", "segments", "phase", "duration", "trans_done", "trans_total")}
                                          for it in j["items"]]}
                             for j in store.data["jobs"][:6]],
                    "ts": time.time(),
                }, ensure_ascii=False)
            if snapshot != last:
                last = snapshot
                yield f"data: {snapshot}\n\n"
            time.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# --------------------------------------------------------------------------- #
# Static
# --------------------------------------------------------------------------- #

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())
