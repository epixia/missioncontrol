# Live Preview of a Coded Project — Design

Status: approved via brainstorming, ready for implementation planning
Date: 2026-08-29

## Purpose

Today, once an agent finishes building something (e.g. `examples/tic-tac-toe/`
in this repo), the only way to see it is to open the files manually outside
Mission Control. This spec adds a "Preview" panel to the mission-detail view
that shows the project's own `index.html`, live, right in the dashboard.

## Decisions made during brainstorming (do not re-litigate without cause)

- **Embedded static-file preview, not a launched dev server.** Mission
  Control serves the workspace's own files directly (an `<iframe>` pointed
  at a new route) rather than starting the project's own server (`npm run
  dev`, `python -m http.server`, etc.). This covers static HTML/CSS/JS
  projects — exactly what this codebase's own example (`tic-tac-toe`)
  is — with no extra process to launch, track, or clean up. It does **not**
  cover projects that need a real backend/build step to render anything
  (a React app needing `npm run dev`, an API-backed page). That's a
  meaningfully different, heavier feature (detecting how to start an
  arbitrary project, managing its lifecycle) and is explicitly out of scope
  here — a candidate for a future spec if it's ever needed.
- **Preview targets whichever task in the mission was created most
  recently.** No new "preview path" field on `Mission` — the existing
  `MissionTask.workspace_path` (already present on every task) is reused as-
  is. Simple, no data-model change, and naturally follows wherever the
  mission's work is currently happening.
- **No auto-reload.** The preview `<iframe>` loads once and stays put; a
  manual "Reload" button re-navigates it. Auto-reloading on the dashboard's
  existing 4-second poll would interrupt anyone actively interacting with
  the preview (e.g. playing a game the Coder just built).

## API

New route in `app.py`, appended near the other mission-scoped routes:

```python
@app.get("/api/missions/{mission_id}/preview/{file_path:path}")
def preview_file(mission_id: str, file_path: str = "") -> FileResponse:
    with get_session() as session:
        mission = session.get(Mission, mission_id)
        if mission is None:
            raise HTTPException(404, "mission not found")
        latest_task = session.exec(
            select(MissionTask)
            .where(MissionTask.mission_id == mission_id)
            .order_by(MissionTask.created_at.desc())
        ).first()
    if latest_task is None:
        raise HTTPException(404, "mission has no tasks yet")

    workspace_root = Path(latest_task.workspace_path).resolve()
    requested = (workspace_root / (file_path or "index.html")).resolve()
    if requested.is_dir():
        requested = requested / "index.html"

    # Path-traversal guard: file_path could contain "..". This is the only
    # route in this app that serves a file by a caller-supplied relative
    # path, so unlike every other route (which trusts the local caller for
    # everything else — full file-write access is already granted to
    # agents), this one specifically stays inside the resolved workspace
    # directory no matter what file_path says.
    if not requested.is_relative_to(workspace_root):
        raise HTTPException(404, "not found")
    if not requested.is_file():
        raise HTTPException(404, "not found — does this project have an index.html?")

    return FileResponse(requested)
```

`FileResponse` (already imported in `app.py` — `from fastapi.responses import
FileResponse, StreamingResponse`) sets `Content-Type` from the file
extension automatically, so `.js`/`.css`/`.html`/images all serve correctly
without extra code.

`file_path` defaults to `""` (FastAPI's `{file_path:path}` matches the empty
string when the URL has no trailing segment, e.g. `GET
/api/missions/{id}/preview`), which the handler maps to `index.html`. This
means the exact same route serves both the entry page and every relative
asset it references (`<script src="script.js">`, `<link href="style.css">`)
— the browser resolves those relative to the page's own URL, so no rewriting
of the project's own HTML is needed. This mirrors how DeepLogic's own `/lab/
<slug>/` convention already documented elsewhere expects relative asset
paths for exactly this reason.

## UI changes

`dashboard.html` gains a new panel, `#previewPanel`, placed after
`#activityPanel` (or wherever reads naturally alongside the other mission-
wide panels — implementer's call, following this file's existing stacked-
panel convention):

```html
<div class="panel" id="previewPanel" style="margin-bottom:0.6rem;">
  <h2>Preview
    <span>
      <button type="button" id="previewReloadBtn">Reload</button>
      <a id="previewOpenLink" href="#" target="_blank">Open in new tab ↗</a>
    </span>
  </h2>
  <iframe id="previewFrame" style="width:100%; height:420px; border:1px solid var(--border); background:#fff;"></iframe>
</div>
```

JS: when a mission is opened (`openMission`), set `previewFrame.src` and
`previewOpenLink.href` to `/api/missions/{missionId}/preview` once. The
Reload button re-assigns `previewFrame.src` to the same URL (forcing a
re-navigation; appending a `?t=${Date.now()}` cache-buster avoids the
browser serving a stale cached copy of `index.html` after new agent edits).
This does **not** hook into the existing 4-second poll loop or
`refreshDetail()` at all — it is deliberately static until the user clicks
Reload, per the brainstorming decision above.

If the workspace has no `index.html`, the iframe will show the browser's
own 404 rendering of the JSON error body — acceptable for v1 (matches how
other empty/error states in this dashboard are minimal); a nicer inline
empty-state message can be a follow-up if it turns out to matter.

## Testing

- **Offline, in default `pytest` run**, using this project's established
  in-memory-DB pattern plus a real temp directory for the file-serving
  parts (this route is the first in the codebase to touch the real
  filesystem in a test, since every other route only touches the DB —
  use `tmp_path`, pytest's built-in temp-directory fixture):
  - Serves `index.html` at the bare `/preview` path (no `file_path`).
  - Serves a nested asset (`style.css`, `script.js`) at
    `/preview/style.css`.
  - 404 when `file_path` contains `..` and would resolve outside the
    workspace root (the path-traversal guard) — construct a case where the
    naive join would escape (e.g. `file_path="../../../../etc/passwd"` or
    a Windows-safe equivalent) and assert 404, not the escaped file's
    contents.
  - 404 with the "does this project have an index.html?" message when the
    workspace directory exists but is empty.
  - 404 when the mission doesn't exist, and when the mission exists but has
    no tasks yet.
  - Uses the most recently created task's `workspace_path` when a mission
    has multiple tasks with different workspace paths (create two tasks
    with different `created_at`/workspace, assert the newer one's file
    wins).
- **Not in default suite, live verification only:** load `examples/tic-tac-
  toe/` in an actual mission and confirm the dashboard's Preview panel
  renders the real page, the game is playable inside the iframe, and
  clicking Reload after an edit picks up the change.

## Explicitly out of scope (do not implement as part of this spec)

- Launching or managing a project's own dev server / build step.
- Directory browsing/listing.
- Auto-reloading the preview on the existing poll cycle.
- An explicit, independently-settable "preview path" on `Mission` (decided
  against — most-recently-created-task's `workspace_path` is used instead).
- A nicer empty-state UI for "no index.html found" (plain 404 in the
  iframe is acceptable for v1).
