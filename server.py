#!/usr/bin/env python3
"""spritz — open-source film set for AI videos.

Single-file backend: project store, generation job queue, engine adapters
(mock + ComfyUI), Blender take import, FCP7 XML timeline export.

Run:  uvicorn server:app --port 4700 --reload
UI:   http://127.0.0.1:4700/
"""
from __future__ import annotations

import json
import queue
import shutil
import subprocess
import threading
import time
import uuid
import xml.sax.saxutils as sx
from pathlib import Path

import httpx
import uvicorn
import re

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent
DATA = ROOT / "data"
ASSETS = DATA / "assets"
OUTPUTS = DATA / "outputs"
BOARDS = DATA / "boards"
STORE = DATA / "store.json"
for d in (DATA, ASSETS, OUTPUTS, BOARDS):
    d.mkdir(parents=True, exist_ok=True)

FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FFPROBE = shutil.which("ffprobe") or "/opt/homebrew/bin/ffprobe"

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
            take = {"job": job_id, **result}
            if shot is not None:
                shot["takes"].append(take)
            _save(DB)
        _on_take_ready(job["project"], job["shot"], take)
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


# ---------------------------------------------------------------- board
# One wall per project: data/boards/{pid}.json = {cards, edges, updated}.
# Cards: {id, type, x, y, w, h, z, title, color?, created, updated, data{}}.
# Edges: {from, to, kind} with kind in references | produced | note.

_board_lock = threading.Lock()
_subs: list[tuple[str, "queue.Queue[str]"]] = []      # (pid, queue) SSE subscribers
_subs_lock = threading.Lock()
ASSET_STATUS: dict[str, str] = {}                     # asset name -> pending|ready|error

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".tif", ".tiff", ".bmp", ".exr", ".heic", ".heif", ".dpx"}
VIDEO_EXT = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v", ".mxf"}
AUDIO_EXT = {".mp3", ".wav", ".aac", ".m4a", ".flac", ".ogg", ".aif", ".aiff"}


PID_RE = re.compile(r"^[a-f0-9]{8}$")


def _safe_pid(pid: str) -> str:
    """Board ids are 8 hex chars (uuid4[:8]); anything else is refused before touching disk."""
    if not PID_RE.match(pid):
        raise HTTPException(400, "invalid project id")
    return pid


def _safe_asset(name: str) -> Path:
    """Reduce a client-supplied name to a basename inside data/assets, or 400."""
    p = (ASSETS / Path(name).name).resolve()
    if not p.is_relative_to(ASSETS.resolve()) or p == ASSETS.resolve():
        raise HTTPException(400, "invalid asset name")
    return p


def _board_path(pid: str) -> Path:
    return BOARDS / f"{_safe_pid(pid)}.json"


def _load_board(pid: str) -> dict:
    p = _board_path(pid)
    if p.exists():
        return json.loads(p.read_text())
    return {"project": pid, "cards": [], "edges": [], "updated": time.time()}


def _save_board(pid: str, board: dict) -> dict:
    board["project"] = pid
    board["updated"] = time.time()
    p = _board_path(pid)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(board, indent=1))
    tmp.replace(p)
    return board


def _emit(pid: str, event: str, data: dict) -> None:
    msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    with _subs_lock:
        for spid, q in _subs:
            if spid == pid or pid == "*":
                q.put(msg)


def _asset_kind(name: str) -> str:
    ext = Path(name).suffix.lower()
    if ext in IMAGE_EXT: return "image"
    if ext in VIDEO_EXT: return "video"
    if ext in AUDIO_EXT: return "audio"
    if ext == ".pdf": return "pdf"
    return "file"


def _probe(path: Path) -> dict:
    """ffprobe -> {width, height, duration}; empty dict on failure."""
    try:
        out = subprocess.run([FFPROBE, "-v", "error", "-print_format", "json",
                              "-show_format", "-show_streams", str(path)],
                             capture_output=True, text=True, timeout=30).stdout
        j = json.loads(out or "{}")
        v = next((st for st in j.get("streams", []) if st.get("codec_type") == "video"), {})
        dur = float(j.get("format", {}).get("duration") or v.get("duration") or 0)
        return {"width": v.get("width"), "height": v.get("height"), "duration": round(dur, 3)}
    except Exception:
        return {}


def _ff(*args: str, timeout: int = 600) -> bool:
    r = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode == 0


def _make_proxies(src: Path, kind: str) -> dict:
    """Master untouched; proxies next to it: .thumb.jpg / .proxy.mp4 / .wave.png."""
    stem = src.name
    thumb = ASSETS / f"{stem}.thumb.jpg"
    out: dict = {}
    if kind == "image":
        ok = _ff("-i", str(src), "-frames:v", "1", "-vf", "scale=512:-2", str(thumb))
        if not ok:  # EXR/HEIC ffmpeg cannot decode: grey placeholder
            _ff("-f", "lavfi", "-i", "color=c=#3a3a3e:s=512x288", "-frames:v", "1", str(thumb))
        out["thumb"] = f"/assets/{thumb.name}"
    elif kind == "video":
        proxy = ASSETS / f"{stem}.proxy.mp4"
        _ff("-ss", "0.5", "-i", str(src), "-frames:v", "1", "-vf", "scale=512:-2", str(thumb)) or \
            _ff("-i", str(src), "-frames:v", "1", "-vf", "scale=512:-2", str(thumb))
        out["thumb"] = f"/assets/{thumb.name}"
        vf = "scale=-2:'min(1080,ih)',format=p010le"
        ok = _ff("-i", str(src), "-an", "-vf", vf, "-c:v", "hevc_videotoolbox", "-profile:v", "main10",
                 "-b:v", "8M", "-tag:v", "hvc1", "-movflags", "+faststart", str(proxy))
        if not ok:
            ok = _ff("-i", str(src), "-an", "-vf", "scale=-2:'min(1080,ih)',format=yuv420p10le",
                     "-c:v", "libx265", "-preset", "fast", "-crf", "24", "-tag:v", "hvc1",
                     "-movflags", "+faststart", str(proxy))
        if ok:
            out["proxy"] = f"/assets/{proxy.name}"
    elif kind == "audio":
        wave = ASSETS / f"{stem}.wave.png"
        if _ff("-i", str(src), "-filter_complex", "showwavespic=s=1024x256:colors=#8fa3bf",
               "-frames:v", "1", str(wave)):
            out["wave"] = f"/assets/{wave.name}"
    elif kind == "pdf":
        # ffmpeg has no PDF decoder; on macOS qlmanage renders the first page.
        try:
            subprocess.run(["qlmanage", "-t", "-s", "512", "-o", str(ASSETS), str(src)],
                           capture_output=True, timeout=60)
            png = ASSETS / f"{stem}.png"
            if png.exists():
                _ff("-i", str(png), str(thumb))
                png.unlink(missing_ok=True)
                out["thumb"] = f"/assets/{thumb.name}"
        except Exception:
            pass
    return out


def _proxy_job(name: str, kind: str) -> None:
    try:
        out = _make_proxies(ASSETS / name, kind)
        ASSET_STATUS[name] = "ready"
        _emit("*", "asset.ready", {"asset": f"/assets/{name}", "name": name, **out})
    except Exception as e:
        ASSET_STATUS[name] = "error"
        _emit("*", "asset.error", {"asset": f"/assets/{name}", "name": name, "error": str(e)})


def _card_defaults(card: dict) -> dict:
    now = time.time()
    card.setdefault("id", uuid.uuid4().hex[:8])
    card.setdefault("type", "text")
    for k, v in (("x", 0), ("y", 0), ("w", 240), ("h", 160), ("z", 0), ("title", ""), ("data", {})):
        card.setdefault(k, v)
    card.setdefault("created", now)
    card["updated"] = now
    return card


def _sync_card(pid: str, card: dict) -> None:
    """scene <-> shot and subject <-> store subject. The store is the source of truth
    for content; the card only carries position. Caller must not hold _lock."""
    proj = DB["projects"].get(pid)
    if not proj:
        return
    d = card.get("data") or {}
    if card["type"] == "scene":
        with _lock:
            shot = _find_shot(pid, card["id"])
            if shot is None:
                shot = {"id": card["id"], "name": card.get("title") or "Scene", "prompt": "", "model": "mock",
                        "camera": "static", "duration": 5, "start_frame": None,
                        "camera_guide": None, "takes": [], "selected_take": None}
                proj["shots"].append(shot)
            allowed = {"prompt", "model", "camera", "duration", "start_frame", "camera_guide", "negative"}
            shot.update({k: v for k, v in d.items() if k in allowed and v is not None})
            if card.get("title"):
                shot["name"] = card["title"]
            _save(DB)
    elif card["type"] == "subject":
        with _lock:
            proj.setdefault("subjects", [])
            sub = next((x for x in proj["subjects"] if x["id"] == card["id"]), None)
            if sub is None:
                sub = {"id": card["id"], "name": card.get("title") or "Subject", "description": "", "refs": []}
                proj["subjects"].append(sub)
            if card.get("title"):
                sub["name"] = card["title"]
            for k in ("description", "refs"):
                if k in d:
                    sub[k] = d[k]
            _save(DB)


def _hydrate(pid: str, board: dict) -> dict:
    """Overlay store content onto scene/subject cards so the wall never shows stale text."""
    proj = DB["projects"].get(pid) or {}
    shots = {s["id"]: s for s in proj.get("shots", [])}
    subs = {s["id"]: s for s in proj.get("subjects", [])}
    for c in board["cards"]:
        if c["type"] == "scene" and c["id"] in shots:
            s = shots[c["id"]]
            c["title"] = s["name"]
            c["data"].update({k: s.get(k) for k in ("prompt", "model", "camera", "duration", "negative")})
            c["data"]["takes"] = len(s["takes"])
        elif c["type"] == "subject" and c["id"] in subs:
            s = subs[c["id"]]
            c["title"] = s["name"]
            c["data"].update({"description": s.get("description", ""), "refs": s.get("refs", [])})
    return board


def _on_take_ready(pid: str, sid: str, take: dict) -> None:
    """Job finished: proxy the output, drop a take card right of its scene, link it, notify."""
    output = take.get("output", "")
    name = Path(output).name
    src = (OUTPUTS / name).resolve()
    if not src.is_relative_to(OUTPUTS.resolve()):
        return
    kind = _asset_kind(name)
    proxies = _make_proxies(src, kind) if kind in ("video", "image") and src.exists() else {}
    params = DB["jobs"].get(take.get("job"), {}).get("params", {})
    with _board_lock:
        board = _load_board(pid)
        scene = next((c for c in board["cards"] if c["id"] == sid), None)
        if scene is None:
            return
        n = sum(1 for e in board["edges"] if e["from"] == sid and e["kind"] == "produced")
        w, h = 260, 200
        card = _card_defaults({
            "id": take["job"], "type": "take", "title": f"Take {n + 1}",
            "x": scene["x"] + scene["w"] + 40, "y": scene["y"] + n * (h + 20), "w": w, "h": h,
            "z": scene.get("z", 0),
            "data": {"job": take["job"], "output": output, "kind": kind, "note": take.get("note"),
                     "model": params.get("model"), "seed": params.get("seed"),
                     "prompt": params.get("prompt", ""), "scene": sid, **proxies}})
        board["cards"].append(card)
        edge = {"from": sid, "to": card["id"], "kind": "produced"}
        board["edges"].append(edge)
        _save_board(pid, board)
    _emit(pid, "take.ready", {"scene": sid, "card": card, "edge": edge})


@app.get("/board", response_class=HTMLResponse)
def board_page() -> str:
    return (ROOT / "app" / "board.html").read_text()


@app.get("/api/projects/{pid}/board")
def get_board(pid: str) -> dict:
    _safe_pid(pid)
    with _board_lock:
        board = _load_board(pid)
        if not _board_path(pid).exists():
            _save_board(pid, board)
    return _hydrate(pid, board)


@app.put("/api/projects/{pid}/board")
def put_board(pid: str, board: dict) -> dict:
    _safe_pid(pid)
    cards = [c if "created" in c else _card_defaults(c) for c in board.get("cards", [])]
    edges = [e for e in board.get("edges", []) if e.get("from") and e.get("to")]
    for c in cards:
        c.setdefault("data", {})
        if c.get("type") in ("scene", "subject"):
            _sync_card(pid, c)
    with _board_lock:
        saved = _save_board(pid, {"cards": cards, "edges": edges})
    return {"updated": saved["updated"], "cards": len(cards), "edges": len(edges)}


@app.patch("/api/projects/{pid}/board/cards")
def patch_cards(pid: str, body: dict) -> dict:
    """ops: [{op: add|update|delete, card: {...}} | {op: add|delete, edge: {from, to, kind}}].
    This is the surface an agent (Claude Code, MCP later) uses to work on the wall."""
    _safe_pid(pid)
    applied = []
    with _board_lock:
        board = _load_board(pid)
        by_id = {c["id"]: c for c in board["cards"]}
        for o in body.get("ops", []):
            op, card, edge = o.get("op"), o.get("card"), o.get("edge")
            if edge:
                if op == "add":
                    board["edges"].append({"from": edge["from"], "to": edge["to"], "kind": edge.get("kind", "note")})
                elif op == "delete":
                    board["edges"] = [e for e in board["edges"]
                                      if not (e["from"] == edge["from"] and e["to"] == edge["to"])]
                applied.append({"op": op, "edge": edge})
                continue
            if not card:
                continue
            if op == "add":
                c = _card_defaults(card)
                if c["id"] in by_id:
                    by_id[c["id"]].update(c); c = by_id[c["id"]]
                else:
                    board["cards"].append(c); by_id[c["id"]] = c
            elif op == "update" and card.get("id") in by_id:
                c = by_id[card["id"]]
                c["data"] = {**c.get("data", {}), **(card.get("data") or {})}
                c.update({k: v for k, v in card.items() if k != "data"})
                c["updated"] = time.time()
            elif op == "delete" and card.get("id") in by_id:
                board["cards"] = [x for x in board["cards"] if x["id"] != card["id"]]
                board["edges"] = [e for e in board["edges"] if card["id"] not in (e["from"], e["to"])]
                by_id.pop(card["id"])
                applied.append({"op": op, "card": {"id": card["id"]}})
                continue
            else:
                continue
            applied.append({"op": op, "card": c})
        _save_board(pid, board)
    for a in applied:
        if a.get("card") and a["card"].get("type") in ("scene", "subject"):
            _sync_card(pid, a["card"])
    _emit(pid, "card.updated", {"ops": applied})
    return {"applied": len(applied), "updated": board["updated"]}


@app.post("/api/board-upload")
async def board_upload(file: UploadFile = File(...)) -> dict:
    """Store the master, answer at once with kind + dimensions, build proxies in a thread."""
    name = f"{uuid.uuid4().hex[:6]}_{Path(file.filename or 'file').name}"
    dest = _safe_asset(name)
    dest.write_bytes(await file.read())
    kind = _asset_kind(name)
    meta = _probe(dest) if kind in ("image", "video", "audio") else {}
    ASSET_STATUS[name] = "pending"
    threading.Thread(target=_proxy_job, args=(name, kind), daemon=True).start()
    return {"asset": f"/assets/{name}", "name": name, "kind": kind, "status": "pending",
            "proxy": None, "thumb": None, "wave": None,
            "width": meta.get("width"), "height": meta.get("height"), "duration": meta.get("duration")}


@app.get("/api/assets/{name}/status")
def asset_status(name: str) -> dict:
    master = _safe_asset(name)
    name = master.name
    st = ASSET_STATUS.get(name)
    if st is None:
        st = "ready" if master.exists() else "missing"
    out = {"asset": f"/assets/{name}", "name": name, "status": st}
    for k, suffix in (("proxy", ".proxy.mp4"), ("thumb", ".thumb.jpg"), ("wave", ".wave.png")):
        p = _safe_asset(f"{name}{suffix}")
        out[k] = f"/assets/{p.name}" if p.exists() else None
    return out


@app.get("/api/projects/{pid}/board/events")
def board_events(pid: str) -> StreamingResponse:
    """SSE: card.updated, asset.ready, take.ready. Keepalive comment every 15 s."""
    _safe_pid(pid)
    q: "queue.Queue[str]" = queue.Queue()
    with _subs_lock:
        _subs.append((pid, q))

    def gen():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                try:
                    yield q.get(timeout=15)
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            with _subs_lock:
                if (pid, q) in _subs:
                    _subs.remove((pid, q))

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/projects/{pid}/subjects")
def create_subject(pid: str, name: str = Form("Subject")) -> dict:
    sub = {"id": uuid.uuid4().hex[:8], "name": name, "description": "", "refs": []}
    with _lock:
        DB["projects"][pid].setdefault("subjects", []).append(sub)
        _save(DB)
    return sub


@app.post("/api/projects/{pid}/subjects/{sub_id}")
def update_subject(pid: str, sub_id: str, patch: dict) -> dict:
    with _lock:
        sub = next(s for s in DB["projects"][pid].get("subjects", []) if s["id"] == sub_id)
        sub.update({k: v for k, v in patch.items() if k in {"name", "description", "refs"}})
        _save(DB)
    return sub


app.mount("/app", StaticFiles(directory=ROOT / "app"), name="app")
app.mount("/assets", StaticFiles(directory=ASSETS), name="assets")
app.mount("/outputs", StaticFiles(directory=OUTPUTS), name="outputs")

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=4700)
