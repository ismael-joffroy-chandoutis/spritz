[English](README.md) · [Français](README.fr.md)

# spritz

An open-source film set for AI videos. Reverse-engineered from [martini.film](https://www.martini.film/) and rebuilt on tools you can run yourself: a director-style interface (shots, camera moves, takes, timeline) driving open video models through ComfyUI, with the same Blender camera handoff — author the camera in Blender, the model renders it and never re-invents the motion.

## What Martini actually is, under the hood

Findings from studying their public surface (app shell, i18n catalog, published agent skills, MCP docs), 19 July 2026:

| Layer | What they run | Evidence |
|---|---|---|
| Frontend | Build-free vanilla JS styled like an Adobe Premiere panel | `app.martini.film` ships `config.js` ("Simple, build-free configuration") and `premiere-design.css` ("tuned to resemble Adobe Premiere panel") |
| Backend | Supabase (auth, DB, storage) + Vercel | supabase-js from CDN, OTP/OAuth sign-in, media on a Supabase public bucket |
| Models | None of their own — an **aggregator** over hosted APIs: Veo, Kling, Sora, Seedance, Hailuo, Grok, Pixverse, Luma, Gemini, LTX, plus image models (Nano Banana, Seedream, Reve, Flux, GPT Image) | `/supported-models`, per-second credit pricing ("olives", $0.10) |
| Camera control | **No 3D engine of their own.** Blender authors the camera; they render a clean EEVEE guide flythrough + first frame + sampled camera path, and hand the guide to Seedance with a "follow the guide's exact trajectory" instruction | their public `blender-to-martini` skill: bundle = `first-frame.png` + `camera-guide.mp4` + `camera-path.json`; "Blender authors the camera; Martini renders it and must never re-invent the motion" |
| "Step into any image" | Image/video → rough 3D "set" → reframe → re-render (same guide mechanism, world-model or depth-based set building) | "Set Creation Completed" / "Camera Control Completed" job types, "3D" view mode, marketing "step into virtual worlds" |
| Handheld takes | iOS app records real camera motion as a take (phone = motion-capture for camera paths) | "Use Martini for iOS to record handheld takes" |
| Agent surface | An MCP server (`martini.film/mcp`) + published agent skills; the app is built to be driven by Claude Code/Codex/Cursor | `github.com/martini-film/skills`, MCP verbs: `create_node_and_generate`, `render_blender_take`, `get_board_scenes`, `list_models`… |
| Consistency | "Subjects": collections of reference stills + text per character/place/prop, referenced as `@tags` in prompts — multi-reference conditioning, no LoRA training | library UI strings, multi-reference role tagging in their playbook |
| Image editing | Auto-mask object selection (SAM-style) + inpaint ("Infill Edit"), annotate, crop | editor strings: "Detecting object…", "Edit everything EXCEPT the painted area" |
| Editorial | Rough-assembly export: XML + media in a ZIP for Premiere/Resolve | "Generating XML… Creating ZIP bundle" |
| Audio | TTS dialogue with speaker labels, music, SFX, section-based audio editing | audio editor strings |

The insight: **Martini's moat is not a model — it is a director-shaped workflow wrapped around other people's models.** Every piece has an open equivalent.

## The open mapping

| Martini capability | spritz equivalent (all open) |
|---|---|
| Hosted video models | Wan 2.2 / LTX-Video / HunyuanVideo on your own GPU via [ComfyUI](https://github.com/comfyanonymous/ComfyUI) |
| Camera-faithful render from Blender | Same bundle contract (`first-frame.png` + `camera-guide.mp4` + `camera-path.json`) via `blender/export_take.py`, rendered with **Wan VACE** control-video conditioning |
| Camera move presets | Prompt-level camera grammar + guide videos; for finer control: CameraCtrl, MotionCtrl, ReCamMaster, Uni3C |
| "Step into an image" set building | Depth Anything → point cloud / gaussian splats (Nerfstudio, gsplat) → Blender camera → guide video |
| Subjects / @references | Reference-image conditioning (IPAdapter, Wan multi-reference) or LoRA per character |
| Auto-mask + infill | Segment Anything + inpainting workflows in ComfyUI |
| TTS / music / SFX | XTTS or Parakeet-class TTS, MusicGen, AudioGen — all local |
| XML export to Premiere/Resolve | Built in (`/api/projects/{id}/export.xml`, FCP7 XML) |
| MCP agent surface | The backend is plain HTTP endpoints — trivially wrappable as MCP tools |

## Run it

```bash
pip install fastapi uvicorn httpx python-multipart
uvicorn server:app --port 4700
# open http://127.0.0.1:4700
```

Point `config.json` at your ComfyUI (copy `config.example.json` to `config.json` and set your ComfyUI URL). Without a rig, the `mock` model exercises the whole flow (projects → shots → generate → takes → timeline → XML).

The ComfyUI workflow templates in `workflows/` are placeholders to adapt to your installed nodes — see `workflows/README.md`.

## Blender handoff

1. Block out the scene, keyframe the camera, run `blender/export_take.py` inside Blender.
2. Drop the exported `spritz-take/` bundle into the right-hand panel of the UI.
3. The shot arrives wired to a VACE control-video workflow: the model follows your exact camera.

## Status

Working skeleton, honestly labeled: UI, project/shot/take store, job queue, mock engine, ComfyUI adapter, Blender exporter and XML export are functional; the workflow JSONs must be adapted to your ComfyUI install before real renders. No cloud, no accounts, your footage stays on your disk.

## License

MIT. Not affiliated with C47 Inc. — the study of their public surface is documented above; no proprietary code or assets are used.
