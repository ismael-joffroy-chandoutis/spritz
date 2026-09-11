# spritz-board MCP

Serveur MCP stdio qui emballe les endpoints HTTP du board. Il ne réimplémente
rien : chaque outil est un appel au serveur spritz qui tourne.

Outils : `list_projects`, `read_board`, `add_card`, `add_note`, `move_card`,
`generate_from_scene`, `list_takes`.

Variables : `SPRITZ_URL` (défaut `http://127.0.0.1:4700`), `SPRITZ_TIMEOUT`.

Enregistrement :

    claude mcp add spritz-board --scope user -- \
      ~/apps/spritz/.venv/bin/python ~/apps/spritz/mcp/spritz_board_mcp.py

Le serveur du board doit tourner, sinon les outils renvoient une erreur de
connexion. Dépendance : `mcp` 2.x (API `MCPServer`, pas `FastMCP`).
