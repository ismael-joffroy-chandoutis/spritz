#!/usr/bin/env python3
"""spritz — open-source film set for AI videos.

Single-file backend: project store, generation job queue, engine adapters
(mock + ComfyUI), Blender take import, FCP7 XML timeline export.

Run:  uvicorn server:app --port 4700 --reload
UI:   http://127.0.0.1:4700/
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import uuid
import xml.sax.saxutils as sx
from pathlib import Path

import httpx
import uvicorn
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent
DATA = ROOT / "data"
ASSETS = DATA / "assets"
OUTPUTS = DATA / "outputs"
STORE = DATA / "store.json"
for d in (DATA, ASSETS, OUTPUTS):
    d.mkdir(parents=True, exist_ok=True)

COMFY_URL = json.loads((ROOT / "config.json").read_text())["comfyui_url"] if (ROOT / "config.json").exists() else "http://127.0.0.1:8188"

# ---------------------------------------------------------------- store

_lock = threading.Lock()


def _load() -> dict:
    if STORE.exists():
        return json.loads(STORE.read_text())
    return {"projects": {}, "jobs": {}}


def _save(db: dict) -> None:
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, indent=1))
    tmp.replace(STORE)


DB = _load()

# ---------------------------------------------------------------- models

# Every entry maps a Martini-style capability onto an open backend.
MODELS = [
    {"id": "wan-i2v", "name": "Wan 2.2 I2V (ComfyUI)", "engine": "comfyui",
     "caps": ["start_frame", "camera_prompt"], "kind": "video",
     "workflow": "workflows/wan_i2v.json"},
    {"id": "wan-vace", "name": "Wan VACE control-video (ComfyUI)", "engine": "comfyui",
     "caps": ["start_frame", "camera_guide"], "kind": "video",
     "workflow": "workflows/wan_vace.json"},
    {"id": "ltx-i2v", "name": "LTX-Video I2V (ComfyUI)", "engine": "comfyui",
     "caps": ["start_frame"], "kind": "video",
     "workflow": "workflows/ltx_i2v.json"},
    {"id": "flux-image", "name": "Flux/SDXL still (ComfyUI)", "engine": "comfyui",
     "caps": ["reference_images"], "kind": "image",
     "workflow": "workflows/flux_image.json"},
    {"id": "mock", "name": "Mock (no GPU — testing)", "engine": "mock",
     "caps": ["start_frame", "camera_guide"], "kind": "video", "workflow": None},
]

CAMERA_MOVES = [
    "static", "slow push-in", "slow pull-out", "pan left", "pan right",
    "tilt up", "tilt down", "orbit left", "orbit right", "dolly left",
    "dolly right", "crane up", "crane down", "handheld", "follow the camera guide exactly",
]

# ---------------------------------------------------------------- engines


def run_mock(job: dict) -> dict:
    time.sleep(2)
    ph = OUTPUTS / f"{job['id']}.txt"
    ph.write_text("mock render: " + job["params"].get("prompt", ""))
    return {"output": f"/outputs/{ph.name}", "note": "mock engine — plug ComfyUI for real renders"}


def run_comfyui(job: dict) -> dict:
    """Inject prompt/init image into a workflow template and run it on ComfyUI.

    Templates use string placeholders: __PROMPT__, __NEGATIVE__, __INIT_IMAGE__,
    __GUIDE_VIDEO__, __SEED__. Adjust the JSON files to the nodes installed on
    your rig (see workflows/README.md).
    """
    p = job["params"]
    model = next(m for m in MODELS if m["id"] == p["model"])
    tpl_path = ROOT / model["workflow"]
    if not tpl_path.exists():
        raise RuntimeError(f"workflow template missing: {model['workflow']}")
    tpl = tpl_path.read_text()

    with httpx.Client(timeout=30) as c:
        # upload conditioning media first, ComfyUI wants server-side filenames
        for key, ph in (("start_frame", "__INIT_IMAGE__"), ("camera_guide", "__GUIDE_VIDEO__")):
            if p.get(key):
                local = ASSETS / Path(p[key]).name
                r = c.post(f"{COMFY_URL}/upload/image",
                           files={"image": (local.name, local.read_bytes())},
                           data={"overwrite": "true"})
                r.raise_for_status()
                tpl = tpl.replace(ph, r.json()["name"])

        camera = p.get("camera") or "static"
        prompt = p.get("prompt", "")
        if camera != "static":
            prompt = f"{prompt}. Camera: {camera}."
        tpl = (tpl.replace("__PROMPT__", json.dumps(prompt)[1:-1])
                  .replace("__NEGATIVE__", json.dumps(p.get("negative", ""))[1:-1])
                  .replace("__SEED__", str(p.get("seed") or int(time.time()))))

        r = c.post(f"{COMFY_URL}/prompt", json={"prompt": json.loads(tpl)})
        r.raise_for_status()
        pid = r.json()["prompt_id"]

    # poll history
    with httpx.Client(timeout=30) as c:
        while True:
            time.sleep(3)
            h = c.get(f"{COMFY_URL}/history/{pid}").json()
            if pid not in h:
                continue
            entry = h[pid]
            if entry.get("status", {}).get("completed") or entry.get("outputs"):
                for node in entry.get("outputs", {}).values():
                    for kind in ("videos", "gifs", "images"):
                        for f in node.get(kind, []):
                            r = c.get(f"{COMFY_URL}/view", params={
                                "filename": f["filename"],
                                "subfolder": f.get("subfolder", ""),
                                "type": f.get("type", "output")})
                            out = OUTPUTS / f"{job['id']}_{f['filename']}"
                            out.write_bytes(r.content)
                            return {"output": f"/outputs/{out.name}"}
                raise RuntimeError("ComfyUI finished without downloadable outputs")
            if entry.get("status", {}).get("status_str") == "error":
                raise RuntimeError(f"ComfyUI error: {json.dumps(entry['status'])[:500]}")


ENGINES = {"mock": run_mock, "comfyui": run_comfyui}


def worker(job_id: str) -> None:
    with _lock:
        job = DB["jobs"][job_id]
        job["status"] = "running"
        _save(DB)
    try:
        engine = next(m for m in MODELS if m["id"] == job["params"]["model"])["engine"]
        result = ENGINES[engine](job)
        with _lock:
            job.update(status="done", result=result)
            shot = _find_shot(job["project"], job["shot"])
            if shot is not None:
                shot["takes"].append({"job": job_id, **result})
            _save(DB)
    except Exception as e:  # surface the real error to the UI, never swallow it
        with _lock:
            job.update(status="error", error=str(e))
            _save(DB)


def _find_shot(project_id: str, shot_id: str) -> dict | None:
    proj = DB["projects"].get(project_id)
    if not proj:
        return None
    return next((s for s in proj["shots"] if s["id"] == shot_id), None)


# ---------------------------------------------------------------- api

app = FastAPI(title="spritz")


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (ROOT / "app" / "index.html").read_text()


@app.get("/api/models")
def models() -> dict:
    return {"models": MODELS, "camera_moves": CAMERA_MOVES, "comfyui": COMFY_URL}


@app.get("/api/state")
def state() -> dict:
    return DB


@app.post("/api/projects")
def create_project(name: str = Form(...)) -> dict:
    pid = uuid.uuid4().hex[:8]
    with _lock:
        DB["projects"][pid] = {"id": pid, "name": name, "shots": [], "subjects": []}
        _save(DB)
    return DB["projects"][pid]


@app.post("/api/projects/{pid}/shots")
def create_shot(pid: str, name: str = Form("Shot")) -> dict:
    shot = {"id": uuid.uuid4().hex[:8], "name": name, "prompt": "", "model": "mock",
            "camera": "static", "duration": 5, "start_frame": None,
            "camera_guide": None, "takes": [], "selected_take": None}
    with _lock:
        DB["projects"][pid]["shots"].append(shot)
        _save(DB)
    return shot


@app.post("/api/projects/{pid}/shots/{sid}")
def update_shot(pid: str, sid: str, patch: dict) -> dict:
    with _lock:
        shot = _find_shot(pid, sid)
        allowed = {"name", "prompt", "model", "camera", "duration",
                   "start_frame", "camera_guide", "selected_take", "negative"}
        shot.update({k: v for k, v in patch.items() if k in allowed})
        _save(DB)
    return shot


@app.post("/api/upload")
async def upload(file: UploadFile = File(...)) -> dict:
    name = f"{uuid.uuid4().hex[:6]}_{Path(file.filename).name}"
    dest = ASSETS / name
    dest.write_bytes(await file.read())
    return {"path": f"/assets/{name}"}


@app.post("/api/projects/{pid}/shots/{sid}/generate")
def generate(pid: str, sid: str) -> dict:
    shot = _find_shot(pid, sid)
    jid = uuid.uuid4().hex[:8]
    params = {k: shot.get(k) for k in
              ("prompt", "negative", "model", "camera", "start_frame", "camera_guide", "duration")}
    with _lock:
        DB["jobs"][jid] = {"id": jid, "project": pid, "shot": sid,
                           "status": "queued", "params": params, "created": time.time()}
        _save(DB)
    threading.Thread(target=worker, args=(jid,), daemon=True).start()
    return DB["jobs"][jid]


@app.post("/api/projects/{pid}/take-import")
async def take_import(pid: str, name: str = Form("Blender take"),
                      first_frame: UploadFile = File(...),
                      camera_guide: UploadFile = File(...),
                      camera_path: UploadFile = File(None)) -> dict:
    """Import a Blender export (first-frame.png + camera-guide.mp4 [+ camera-path.json])
    as a new shot wired for guide-following generation. Same bundle contract as
    blender/export_take.py produces."""
    shot = create_shot(pid, name)
    saved = {}
    for key, up in (("start_frame", first_frame), ("camera_guide", camera_guide)):
        fname = f"{shot['id']}_{Path(up.filename).name}"
        (ASSETS / fname).write_bytes(await up.read())
        saved[key] = f"/assets/{fname}"
    meta = {}
    if camera_path is not None:
        raw = await camera_path.read()
        (ASSETS / f"{shot['id']}_camera-path.json").write_bytes(raw)
        meta = json.loads(raw)
    with _lock:
        shot.update(saved, model="wan-vace", camera="follow the camera guide exactly",
                    duration=round(meta.get("frame_count", 120) / meta.get("fps", 24), 1))
        _save(DB)
    return shot


@app.get("/api/projects/{pid}/export.xml")
def export_xml(pid: str) -> Response:
    """FCP7 XML rough assembly — opens in Premiere and Resolve."""
    proj = DB["projects"][pid]
    fps = 24
    clips, t = [], 0
    for i, s in enumerate(proj["shots"]):
        frames = int(s["duration"] * fps)
        take = next((tk for tk in s["takes"] if tk["job"] == s.get("selected_take")),
                    s["takes"][-1] if s["takes"] else None)
        path = str((ROOT / "data" / take["output"].lstrip("/")).resolve()) if take else ""
        clips.append(f"""
      <clipitem id="clip-{i}">
        <name>{sx.escape(s['name'])}</name>
        <duration>{frames}</duration>
        <rate><timebase>{fps}</timebase><ntsc>FALSE</ntsc></rate>
        <start>{t}</start><end>{t + frames}</end>
        <in>0</in><out>{frames}</out>
        <file id="file-{i}">
          <name>{sx.escape(Path(path).name or s['name'])}</name>
          <pathurl>file://{sx.escape(path)}</pathurl>
        </file>
      </clipitem>""")
        t += frames
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE xmeml>
<xmeml version="4">
  <sequence>
    <name>{sx.escape(proj['name'])}</name>
    <duration>{t}</duration>
    <rate><timebase>{fps}</timebase><ntsc>FALSE</ntsc></rate>
    <media><video>
      <format><samplecharacteristics>
        <rate><timebase>{fps}</timebase></rate>
        <width>1280</width><height>720</height>
      </samplecharacteristics></format>
      <track>{''.join(clips)}
      </track>
    </video></media>
  </sequence>
</xmeml>"""
    return Response(content=xml, media_type="application/xml",
                    headers={"Content-Disposition": f'attachment; filename="{proj["name"]}.xml"'})


@app.get("/api/comfy/health")
def comfy_health() -> dict:
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{COMFY_URL}/system_stats")
            return {"ok": r.status_code == 200, "stats": r.json()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")
app.mount("/outputs", StaticFiles(directory=OUTPUTS), name="outputs")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=4700)
