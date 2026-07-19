# Workflow templates

The ComfyUI adapter in `server.py` reads one of these JSON files (API format,
not the UI format), replaces the placeholders below, and POSTs it to
`COMFY_URL/prompt`:

| Placeholder | Replaced with |
|---|---|
| `__PROMPT__` | shot prompt (+ camera move appended) |
| `__NEGATIVE__` | negative prompt |
| `__INIT_IMAGE__` | server-side filename of the uploaded start frame |
| `__GUIDE_VIDEO__` | server-side filename of the uploaded camera-guide video |
| `__SEED__` | random seed |

These templates are **starting points, not drop-in guarantees**: node names and
model checkpoints must match what is installed on your ComfyUI. Export a working
workflow from your own ComfyUI (`Workflow → Export (API)`), put the placeholders
into the right widget values, and save it here under the same name.

- `wan_i2v.json` — Wan 2.2 image-to-video. Needs the Wan checkpoints + WanVideo nodes.
- `wan_vace.json` — Wan VACE with a control video: this is the camera-guide path.
  The guide video from Blender goes in as the VACE control input, the first frame
  as the reference image. This is the open-source equivalent of "the model follows
  the exact camera you keyframed".
- `ltx_i2v.json` — LTX-Video image-to-video (the same open model family Martini
  itself lists as LTX 2.3).
- `flux_image.json` — still generation for start frames / concept boards.
