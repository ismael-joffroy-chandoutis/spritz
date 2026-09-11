[English](README.md) · [Français](README.fr.md)

# spritz

Un plateau de tournage open source pour la vidéo IA. Rétro-conçu depuis [martini.film](https://www.martini.film/) et reconstruit sur des outils que vous faites tourner vous-même : une interface de réalisateur (plans, mouvements de caméra, prises, timeline) qui pilote des modèles vidéo ouverts via ComfyUI, avec le même passage de relais Blender — la caméra s'écrit dans Blender, le modèle la rend et ne réinvente jamais le mouvement.

## Ce que Martini est vraiment, sous le capot

Constats issus de l'étude de leur surface publique (shell de l'app, catalogue i18n, skills agent publiés, docs MCP), 19 juillet 2026 :

| Couche | Ce qu'ils font tourner | Indice |
|---|---|---|
| Frontend | JS vanilla sans build, stylé comme un panneau Adobe Premiere | `app.martini.film` sert `config.js` (« Simple, build-free configuration ») et `premiere-design.css` (« tuned to resemble Adobe Premiere panel ») |
| Backend | Supabase (auth, base, stockage) + Vercel | supabase-js en CDN, connexion OTP/OAuth, médias sur bucket public Supabase |
| Modèles | Aucun modèle à eux — un **agrégateur** d'API hébergées : Veo, Kling, Sora, Seedance, Hailuo, Grok, Pixverse, Luma, Gemini, LTX, plus les modèles image (Nano Banana, Seedream, Reve, Flux, GPT Image) | `/supported-models`, tarification au crédit à la seconde (« olives », 0,10 $) |
| Contrôle caméra | **Pas de moteur 3D à eux.** Blender écrit la caméra ; ils rendent un survol-guide EEVEE propre + première image + trajectoire échantillonnée, et donnent le guide à Seedance avec l'instruction de suivre exactement cette trajectoire | leur skill public `blender-to-martini` : bundle = `first-frame.png` + `camera-guide.mp4` + `camera-path.json` ; « Blender authors the camera; Martini renders it and must never re-invent the motion » |
| « Step into any image » | Image/vidéo → « set » 3D grossier → recadrage → nouveau rendu (même mécanisme de guide, construction du set par world model ou profondeur) | types de jobs « Set Creation Completed » / « Camera Control Completed », mode « 3D », marketing « step into virtual worlds » |
| Prises à la main | L'app iOS enregistre le mouvement réel du téléphone comme une prise (le téléphone = capture de trajectoire caméra) | « Use Martini for iOS to record handheld takes » |
| Surface agent | Un serveur MCP (`martini.film/mcp`) + skills agent publiés ; l'app est faite pour être pilotée par Claude Code/Codex/Cursor | `github.com/martini-film/skills`, verbes MCP : `create_node_and_generate`, `render_blender_take`, `get_board_scenes`, `list_models`… |
| Cohérence | « Subjects » : collections d'images de référence + texte par personnage/lieu/accessoire, référencées en `@tags` dans les prompts — conditionnement multi-références, pas d'entraînement LoRA | strings de la bibliothèque, tagging des références par rôle dans leur playbook |
| Retouche image | Sélection d'objet auto (façon SAM) + inpainting (« Infill Edit »), annotation, recadrage | strings de l'éditeur : « Detecting object… », « Edit everything EXCEPT the painted area » |
| Montage | Export d'ours : XML + médias en ZIP pour Premiere/Resolve | « Generating XML… Creating ZIP bundle » |
| Audio | Dialogues TTS avec locuteurs, musique, bruitages, montage audio par sections | strings de l'éditeur audio |

Le constat : **la valeur de Martini n'est pas un modèle — c'est un workflow en forme de réalisateur enroulé autour des modèles des autres.** Chaque pièce a un équivalent ouvert.

## La carte ouverte

| Capacité Martini | Équivalent spritz (tout ouvert) |
|---|---|
| Modèles vidéo hébergés | Wan 2.2 / LTX-Video / HunyuanVideo sur votre propre GPU via [ComfyUI](https://github.com/comfyanonymous/ComfyUI) |
| Rendu fidèle à la caméra Blender | Même contrat de bundle (`first-frame.png` + `camera-guide.mp4` + `camera-path.json`) via `blender/export_take.py`, rendu en conditionnement vidéo-contrôle **Wan VACE** |
| Presets de mouvements caméra | Grammaire caméra au prompt + vidéos-guides ; pour plus fin : CameraCtrl, MotionCtrl, ReCamMaster, Uni3C |
| « Step into an image » | Depth Anything → nuage de points / gaussian splats (Nerfstudio, gsplat) → caméra Blender → vidéo-guide |
| Subjects / @références | Conditionnement par images de référence (IPAdapter, multi-références Wan) ou LoRA par personnage |
| Auto-masque + inpainting | Segment Anything + workflows d'inpainting ComfyUI |
| TTS / musique / bruitages | TTS classe XTTS ou Parakeet, MusicGen, AudioGen — tout en local |
| Export XML Premiere/Resolve | Intégré (`/api/projects/{id}/export.xml`, XML FCP7) |
| Surface agent MCP | Le backend est de simples endpoints HTTP — trivialement enveloppables en outils MCP |

## Lancer

```bash
pip install fastapi uvicorn httpx python-multipart
uvicorn server:app --port 4700
# ouvrir http://127.0.0.1:4700
```

Pointez `config.json` vers votre ComfyUI (copiez `config.example.json` vers `config.json` et renseignez l'URL de votre ComfyUI). Sans rig, le modèle `mock` exerce tout le flux (projets → plans → génération → prises → timeline → XML).

Les gabarits de workflows ComfyUI dans `workflows/` sont à adapter aux nœuds installés chez vous — voir `workflows/README.md`.

## Passage de relais Blender

1. Bloquez la scène, keyframez la caméra, lancez `blender/export_take.py` dans Blender.
2. Déposez le bundle `spritz-take/` exporté dans le panneau droit de l'interface.
3. Le plan arrive câblé sur un workflow VACE à vidéo-contrôle : le modèle suit votre caméra exacte.

## Board

Un mur infini à côté de la liste de plans : `http://127.0.0.1:4700/board`. On y dépose depuis le Finder images, vidéos (ProRes et MOV alpha compris), sons, PDF, liens et notes ; les masters restent intacts dans `data/assets/`, le mur n'affiche que des proxies automatiques (HEVC 10 bits 1080p, vignettes 512 px, formes d'onde). Les cartes subject (`@nom`) et scène correspondent aux subjects et aux plans de spritz ; Générer sur une scène lance le moteur habituel et la prise arrive en carte reliée par SSE. Tout vit dans `data/boards/{projet}.json`, et chaque action est un endpoint HTTP (`PUT /api/projects/{id}/board`, `PATCH .../board/cards`, `POST /api/board-upload`, `GET .../board/events`) pour qu'un agent puisse travailler sur le mur aussi. Spécification et trajectoire : `docs/BOARD-SPEC.md`.

## État

Squelette fonctionnel, étiqueté honnêtement : interface, stockage projets/plans/prises, file de jobs, moteur mock, adapter ComfyUI, exporteur Blender et export XML fonctionnent ; les JSON de workflows doivent être adaptés à votre installation ComfyUI avant tout rendu réel. Pas de cloud, pas de comptes, vos images restent sur votre disque.

## Licence

MIT. Sans affiliation avec C47 Inc. — l'étude de leur surface publique est documentée ci-dessus ; aucun code ni asset propriétaire n'est utilisé.
