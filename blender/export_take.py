"""spritz Blender exporter — author the camera in Blender, render it with AI.

Run inside Blender (Scripting tab, or `blender -b scene.blend -P export_take.py`).
Renders the active keyframed camera as a clean EEVEE guide flythrough and writes
a bundle you drop into the spritz UI (right panel, "Blender take"):

    <output>/first-frame.png    frame 0 of the guide, pixel-consistent
    <output>/camera-guide.mp4   H.264 viewport render along the authored camera
    <output>/camera-path.json   sampled world transforms + lens per frame

The principle: Blender authors the camera; the video model renders it and must
never re-invent the motion. The guide video becomes the control video of a
Wan VACE (or equivalent) workflow.

Clean-room implementation of the bundle format used by Blender→AI camera
handoffs; no third-party code.
"""
import json
from pathlib import Path

import bpy

OUTPUT_DIR = Path(bpy.path.abspath("//spritz-take"))  # next to the .blend
MAX_FRAMES = 240


def main() -> None:
    scene = bpy.context.scene
    cam = scene.camera
    if cam is None:
        raise SystemExit("No active camera — set one (Ctrl+Numpad0) and keyframe it.")

    f0, f1 = scene.frame_start, min(scene.frame_end, scene.frame_start + MAX_FRAMES - 1)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # -- sample the camera path -------------------------------------------
    saved_frame = scene.frame_current
    frames = []
    for f in range(f0, f1 + 1):
        scene.frame_set(f)
        m = cam.matrix_world
        loc, rot = m.to_translation(), m.to_quaternion()
        frames.append({
            "frame": f,
            "location": [loc.x, loc.y, loc.z],
            "rotation_quaternion": [rot.w, rot.x, rot.y, rot.z],
            "lens_mm": cam.data.lens,
        })
    scene.frame_set(saved_frame)

    (OUTPUT_DIR / "camera-path.json").write_text(json.dumps({
        "fps": scene.render.fps,
        "frame_count": len(frames),
        "resolution": [scene.render.resolution_x, scene.render.resolution_y],
        "camera": cam.name,
        "frames": frames,
    }, indent=1))

    # -- render the guide (EEVEE, H.264) ----------------------------------
    r = scene.render
    saved = {k: getattr(r, k) for k in ("filepath", "engine", "fps")}
    saved_end, saved_fmt = scene.frame_end, r.image_settings.file_format
    try:
        r.engine = "BLENDER_EEVEE_NEXT" if hasattr(bpy.types, "RenderEngine") and "BLENDER_EEVEE_NEXT" in [
            e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items] else "BLENDER_EEVEE"
        scene.frame_end = f1
        r.image_settings.file_format = "FFMPEG"
        r.ffmpeg.format = "MPEG4"
        r.ffmpeg.codec = "H264"
        r.filepath = str(OUTPUT_DIR / "camera-guide.mp4")
        bpy.ops.render.render(animation=True)

        # first frame as PNG, from the same render settings for pixel consistency
        scene.frame_set(f0)
        r.image_settings.file_format = "PNG"
        r.filepath = str(OUTPUT_DIR / "first-frame.png")
        bpy.ops.render.render(write_still=True)
    finally:
        for k, v in saved.items():
            setattr(r, k, v)
        scene.frame_end = saved_end
        r.image_settings.file_format = saved_fmt
        scene.frame_set(saved_frame)

    print(f"spritz take exported → {OUTPUT_DIR}")
    print("Drop first-frame.png + camera-guide.mp4 + camera-path.json into the spritz UI.")


main()
