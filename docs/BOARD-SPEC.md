**Version : v2026-09-11.1 · 11/09/2026 02h35 Paris · source de vérité : ce fichier · session : Krea reverse → canevas souverain**

# spritz Board — spécification v0 (étage 1) + trajectoire v1/v2

## Pourquoi
spritz (`~/Projects/Dev/spritz`, FastAPI monofichier + `app/index.html` vanilla JS sans build) est déjà un Martini open source : projets, plans, prises, jobs ComfyUI, import Blender, export XML. Il lui manque la **surface spatiale** : un canevas infini où l'auteur pose vidéos, images, textes, sons, personnages, décors, scènes, et d'où il génère. Comparatif du 11/09 (artifact 99499d6b) : aucun outil existant ne fait ça en local, tous formats, piloté par Claude. Donc on le construit.

Modèles de référence : Martini (MCP : lire le mur, notes, générer, bins, brouillons, multi-agents), Apple Freeform (zéro interface), Flora (une carte reliée à une case relance).

## Principes non négociables
1. **Build-free**, comme le reste de spritz : une page `app/board.html` + `app/board.js` + `app/board.css`, aucun bundler, aucune dépendance npm. Canevas infini fait main (pan/zoom par transform CSS, cartes en position absolue). Pas de xyflow : on n'a pas de chaîne de build et on ne veut pas en avoir.
2. **Local et souverain** : tout vit dans `data/` sur le 4T. Le board d'un projet = `data/boards/{pid}.json`, lisible, versionnable.
3. **Masters intouchés, proxies automatiques** : tout média déposé garde son fichier d'origine dans `data/assets/`. Un proxy est généré à côté (`{name}.proxy.mp4` HEVC 10 bits 1080p pour la vidéo, `{name}.thumb.jpg` 512 px pour image et vidéo, `{name}.wave.png` pour l'audio). Le board n'affiche que les proxies. ffmpeg est sur `/opt/homebrew/bin/ffmpeg`. ProRes, MOV alpha, EXR (via ffmpeg si possible, sinon vignette « EXR » grise) acceptés en entrée.
4. **Zéro coût sans go** : v0 ne lance aucune API payante. Génération = moteurs déjà configurés dans spritz (mock, ComfyUI).
5. **Pilotable par Claude Code** : toute action du board passe par des endpoints HTTP documentés ; un MCP les emballera en v2.
6. **Un `board.json` = une source de vérité**, jamais deux fichiers pour un même mur.

## v0 = étage 1 : le mur (à livrer par cet agent)

### Types de cartes
| type | contenu | rendu |
|---|---|---|
| `image` | asset image | vignette, clic = plein écran |
| `video` | asset vidéo | proxy avec lecteur, scrub à la souris, muet par défaut |
| `audio` | asset audio | forme d'onde + play |
| `text` | markdown court | textarea éditable en place |
| `pdf` | asset PDF | 1re page en vignette (ffmpeg ne fait pas PDF : utiliser `sips` ou `qlmanage -t` sur macOS, sinon icône) |
| `link` | URL | titre + favicon |
| `subject` | personnage/décor/accessoire : nom, description, images de référence (liste d'assets) | carte avec grille de vignettes, alias `@nom` |
| `scene` | texte de scène + références vers cartes `subject` + prompt + moteur + prises | carte avec champ prompt, bouton Générer (v0 : appelle `/api/projects/{pid}/shots/{sid}/generate` existant, moteur mock ou ComfyUI), les prises rendues apparaissent en cartes `take` reliées |
| `take` | prise rendue (job spritz) : proxy + seed + modèle + prompt | carte vidéo avec badge des paramètres |
| `group` | bin : rectangle nommé qui contient d'autres cartes (déplacement groupé) | cadre titré |

Toute carte : `id, type, x, y, w, h, z, title, color?, created, updated, data{}`. Liens : `edges: [{from, to, kind}]` où kind ∈ `references` (scene→subject), `produced` (scene→take), `note` (libre).

### Gestes
- Pan : espace+glisser ou molette ; zoom : ctrl/cmd+molette, centré sur le curseur ; fit : touche `F`.
- Glisser-déposer depuis le Finder ou une autre fenêtre : fichiers → upload → proxy → carte posée sous le curseur. Plusieurs fichiers = grille.
- Coller (cmd+V) une image ou une URL crée une carte.
- Double-clic sur le vide : carte texte. `T` texte, `B` bin, `S` subject, `N` scène (raccourcis Martini).
- Sélection lasso, multi-déplacement, suppression avec confirmation (les assets ne sont jamais supprimés du disque, seule la carte).
- Redimensionnement par la poignée bas-droite, ratio conservé pour image/vidéo.
- Sauvegarde automatique (debounce 500 ms) vers `PUT /api/projects/{pid}/board`. Indicateur « enregistré » discret.
- Undo/redo mémoire (20 niveaux) sur la structure du board.

### Endpoints à ajouter dans `server.py` (même fichier, même style)
- `GET /board` → `app/board.html`
- `GET /api/projects/{pid}/board` → board.json (créé vide si absent)
- `PUT /api/projects/{pid}/board` → remplace, écrit atomiquement (tmp + replace), stocke `updated`
- `PATCH /api/projects/{pid}/board/cards` → liste d'opérations `{op: add|update|delete, card}` (c'est ce que Claude utilisera)
- `POST /api/board-upload` → multipart, écrit l'asset, lance la génération de proxy en thread, renvoie `{asset, proxy, thumb, kind, width, height, duration}` ; proxy peut arriver après : la carte affiche la vignette d'abord, puis passe au proxy quand `GET /api/assets/{name}/status` dit `ready`
- `GET /api/projects/{pid}/board/events` → SSE : `card.updated`, `asset.ready`, `take.ready` (la fin d'un job spritz émet `take.ready` avec l'id de la scène)
- Servir `data/assets` et `data/outputs` en statique (déjà le cas pour outputs, vérifier assets)

### Intégration spritz existante
- Une carte `scene` correspond à un `shot` spritz (même id). Créer une scène crée le shot ; modifier prompt/modèle/durée patche le shot via l'endpoint existant.
- Générer = endpoint existant. Quand `worker()` termine un job, appeler un hook `_on_take_ready(pid, sid, take)` qui : génère proxy + thumb de la sortie, ajoute une carte `take` à droite de la scène (x+w+40), ajoute l'edge `produced`, émet l'événement SSE.
- Les `subjects` spritz (déjà dans le store) deviennent les cartes `subject` ; garder les deux en synchro (subject = source de vérité dans store, la carte ne porte que la position).

### Design
Sobre, sombre par défaut, thème clair respecté par `prefers-color-scheme`. Fond gris neutre, grille pointillée fine au zoom > 0.5. Cartes sans ombre lourde, bord 1 px, radius 6. Police système. Pas d'icônes emoji. Un seul accent (bleu désaturé) pour la sélection. Le canevas doit rester lisible avec 300 cartes : virtualiser l'affichage hors viewport (ne pas monter les `<video>` hors écran).

### Vérification exigée (GATE EXPÉRIENCE)
1. `uvicorn server:app --port 4700` démarre sans erreur, `curl -s localhost:4700/board | head -1` contient `<meta charset="utf-8">`.
2. Test scripté via Playwright ou `browse` : déposer 1 image, 1 vidéo ProRes (générer un MOV ProRes de 3 s avec ffmpeg `-c:v prores_ks`), 1 mp3, 1 texte ; recharger la page ; les 4 cartes sont là, la vidéo affiche son proxy `.proxy.mp4` (vérifier `ffprobe` : hevc, 1080p), le board.json sur disque contient 4 cartes.
3. Créer une scène, un subject, les relier, cliquer Générer avec le moteur mock : une carte `take` apparaît à droite sans recharger la page (SSE).
4. Capture d'écran finale du board avec les cartes, enregistrée dans `docs/board-v0.png`.
5. Rapport final : liste des fichiers créés/modifiés avec `ls -la`, sortie des tests, ce qui n'est PAS fait.

### Hors v0 (ne pas faire, mais ne pas empêcher)
- **v1 collab** : le board servi depuis le Mini sur le tailnet, SSE déjà en place, verrou optimiste par `updated` (409 si périmé), curseurs des autres en couleur. Pas de compte.
- **v2 génération élargie** : fal (sous go, prix affiché), MCP Krea, apps Pinokio comme moteurs dans le sélecteur ; brouillons de prompt sans lancer ; « plans manquants » proposés par Claude.
- **v2 MCP** : serveur MCP `spritz-board` qui emballe les endpoints : `read_board`, `add_card`, `add_note`, `generate_from_scene`, `group_cards`, `list_missing_shots`. Plusieurs agents sur le même board.
- Timeline : reste dans `index.html` et l'export XML existant ; le board n'en fait pas.

## Contraintes de repo
- Branche `feat/board` depuis `feat/rendu-reel-torrent` (branche courante), dans un worktree séparé. Commits conventionnels, petits, jamais de rebase/reset. Ne pas toucher aux workflows ComfyUI ni à `index.html` au-delà d'un lien « Board » dans son en-tête.
- README.md et README.fr.md : section « Board » courte, bilingue, même ton.
- `~/.claude/TOOLS.md` : une ligne ajoutée décrivant `GET /board`.
