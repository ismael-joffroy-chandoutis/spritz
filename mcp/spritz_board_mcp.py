#!/usr/bin/env python3
"""MCP server for the spritz board.

Thin wrapper over the board's HTTP endpoints: every tool is one or two calls to
the running spritz server, nothing is reimplemented here. Point it elsewhere
with SPRITZ_URL (default http://127.0.0.1:4700).
"""
import os
import uuid

import httpx
from mcp.server.mcpserver import MCPServer

BASE = os.environ.get("SPRITZ_URL", "http://127.0.0.1:4700").rstrip("/")
TIMEOUT = float(os.environ.get("SPRITZ_TIMEOUT", "30"))

mcp = MCPServer("spritz-board")


def _get(path: str) -> dict:
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.get(f"{BASE}{path}")
        r.raise_for_status()
        return r.json()


def _post(path: str, **kw) -> dict:
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.post(f"{BASE}{path}", **kw)
        r.raise_for_status()
        return r.json()


def _patch_cards(pid: str, ops: list) -> dict:
    with httpx.Client(timeout=TIMEOUT) as c:
        r = c.patch(f"{BASE}/api/projects/{pid}/board/cards", json={"ops": ops})
        r.raise_for_status()
        return r.json()


@mcp.tool()
def list_projects() -> list:
    """List spritz projects: id, name, and how many shots each holds."""
    state = _get("/api/state")
    return [{"id": p["id"], "name": p.get("name"), "shots": len(p.get("shots", []))}
            for p in state.get("projects", {}).values()]


@mcp.tool()
def read_board(pid: str) -> dict:
    """Read the whole wall of a project: cards and edges, content already hydrated."""
    return _get(f"/api/projects/{pid}/board")


@mcp.tool()
def add_card(pid: str, type: str, title: str = "", x: float = 0, y: float = 0,
             data: dict | None = None, card_id: str = "") -> dict:
    """Add a card to the wall.

    type is one of image, video, audio, text, pdf, link, subject, scene, take, group.
    A scene card also creates the matching spritz shot; put prompt/model/negative
    in data. Returns the id it was filed under.
    """
    cid = card_id or uuid.uuid4().hex[:8]
    card = {"id": cid, "type": type, "title": title, "x": x, "y": y,
            "data": data or {}}
    res = _patch_cards(pid, [{"op": "add", "card": card}])
    return {"id": cid, **res}


@mcp.tool()
def add_note(pid: str, text: str, x: float = 0, y: float = 0) -> dict:
    """Drop a text note on the wall at (x, y)."""
    return add_card(pid, "text", title="", x=x, y=y, data={"text": text})


@mcp.tool()
def move_card(pid: str, card_id: str, x: float, y: float) -> dict:
    """Move one card to (x, y)."""
    return _patch_cards(pid, [{"op": "update", "card": {"id": card_id, "x": x, "y": y}}])


@mcp.tool()
def generate_from_scene(pid: str, scene_card_id: str) -> dict:
    """Run the scene card's prompt on its engine. Returns the queued job.

    The render happens on the configured ComfyUI rig; when it lands the server
    files a take card next to the scene on its own.
    """
    return _post(f"/api/projects/{pid}/shots/{scene_card_id}/generate")


@mcp.tool()
def list_takes(pid: str, scene_card_id: str = "") -> list:
    """List take cards on the wall, optionally only those produced by one scene."""
    board = _get(f"/api/projects/{pid}/board")
    takes = [c for c in board.get("cards", []) if c.get("type") == "take"]
    if scene_card_id:
        takes = [t for t in takes if (t.get("data") or {}).get("scene") == scene_card_id]
    return takes


if __name__ == "__main__":
    mcp.run()
