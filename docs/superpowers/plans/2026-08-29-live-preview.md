# Live Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show a coded project's own `index.html`, live, in a new "Preview" panel on the mission-detail dashboard.

**Architecture:** One new FastAPI route that serves files out of the workspace directory of whichever task in a mission was created most recently, guarded against path traversal; a new dashboard panel with an `<iframe>` pointed at it, loaded once when a mission opens and re-navigated only on an explicit "Reload" click.

**Tech Stack:** FastAPI (`FileResponse`), vanilla JS (no build step) — same stack as the rest of Mission Control, no new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-29-live-preview-design.md`

## Global Constraints

- Embedded static-file preview only — no launching or managing a project's own dev server/build step.
- Preview targets the `workspace_path` of whichever `MissionTask` in the mission has the latest `created_at` — no new field on `Mission`.
- No auto-reload: the `<iframe>` is set once per mission-open; only a manual "Reload" click re-navigates it. It must not be wired into the existing 4-second poll loop or `refreshDetail()`.
- Path-traversal guard: the resolved file path must stay inside the resolved workspace root, or the route 404s. This is the only route in this app that serves a file by a caller-supplied relative path.
- No directory listing, no new data model, no auth (matches every existing route in this local, single-user tool).

---

### Task 1: Preview file-serving route

**Files:**
- Modify: `src/mission_control/server/app.py`
- Test: `tests/test_preview_route.py`

**Interfaces:**
- Consumes: `Mission`, `MissionTask`, `get_session`, `select`, `HTTPException`, `FileResponse`, `Path` — all already imported in `app.py`. No new imports needed.
- Produces: `preview_file(mission_id: str, file_path: str = "") -> FileResponse`, mounted at `GET /api/missions/{mission_id}/preview/{file_path:path}`. Task 2's `<iframe>` and asset requests hit this route by URL — the exact path shape (`/api/missions/{id}/preview` for the bare page, `/api/missions/{id}/preview/<anything>` for assets) must match verbatim.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_preview_route.py
"""Offline tests for GET /api/missions/{id}/preview/{file_path} — serves a
project's own index.html and assets out of the workspace directory of
whichever task in the mission was created most recently. Uses this
project's established in-memory-DB pattern plus pytest's tmp_path for real
files on disk (the only route in this app that needs both)."""

import pytest
from fastapi import HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session, SQLModel, create_engine

from mission_control.server import app as app_module
from mission_control.server.models import Mission, MissionTask


def _make_engine(monkeypatch):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(app_module, "get_session", lambda: Session(engine))
    return engine


def _make_mission_with_task(monkeypatch, workspace_path: str) -> str:
    _make_engine(monkeypatch)
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        task = MissionTask(mission_id=mission.id, runtime="claude_code", prompt="p", workspace_path=workspace_path)
        session.add(task)
        session.commit()
        return mission.id


def test_preview_serves_index_html_at_bare_path(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    mission_id = _make_mission_with_task(monkeypatch, str(tmp_path))

    response = app_module.preview_file(mission_id, "")

    assert isinstance(response, FileResponse)
    assert response.path == str(tmp_path / "index.html")


def test_preview_serves_a_nested_asset(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    (tmp_path / "style.css").write_text("body { color: red; }")
    mission_id = _make_mission_with_task(monkeypatch, str(tmp_path))

    response = app_module.preview_file(mission_id, "style.css")

    assert response.path == str(tmp_path / "style.css")


def test_preview_404_on_path_traversal_outside_workspace(tmp_path, monkeypatch):
    (tmp_path / "index.html").write_text("<h1>hi</h1>")
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("do not serve me")
    mission_id = _make_mission_with_task(monkeypatch, str(tmp_path))

    with pytest.raises(HTTPException) as excinfo:
        app_module.preview_file(mission_id, f"../{secret.name}")
    assert excinfo.value.status_code == 404


def test_preview_404_when_no_index_html(tmp_path, monkeypatch):
    mission_id = _make_mission_with_task(monkeypatch, str(tmp_path))

    with pytest.raises(HTTPException) as excinfo:
        app_module.preview_file(mission_id, "")
    assert excinfo.value.status_code == 404


def test_preview_404_on_unknown_mission(monkeypatch):
    _make_engine(monkeypatch)
    with pytest.raises(HTTPException) as excinfo:
        app_module.preview_file("does-not-exist", "")
    assert excinfo.value.status_code == 404


def test_preview_404_when_mission_has_no_tasks(monkeypatch):
    _make_engine(monkeypatch)
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        mission_id = mission.id

    with pytest.raises(HTTPException) as excinfo:
        app_module.preview_file(mission_id, "")
    assert excinfo.value.status_code == 404


def test_preview_uses_most_recently_created_tasks_workspace(tmp_path, monkeypatch):
    older_dir = tmp_path / "older"
    older_dir.mkdir()
    (older_dir / "index.html").write_text("old")
    newer_dir = tmp_path / "newer"
    newer_dir.mkdir()
    (newer_dir / "index.html").write_text("new")

    _make_engine(monkeypatch)
    with app_module.get_session() as session:
        mission = Mission(name="m")
        session.add(mission)
        session.commit()
        session.refresh(mission)
        older_task = MissionTask(
            mission_id=mission.id, runtime="claude_code", prompt="p", workspace_path=str(older_dir)
        )
        session.add(older_task)
        session.commit()
        mission_id = mission.id

    with app_module.get_session() as session:
        newer_task = MissionTask(
            mission_id=mission_id, runtime="claude_code", prompt="p", workspace_path=str(newer_dir)
        )
        session.add(newer_task)
        session.commit()

    response = app_module.preview_file(mission_id, "")
    assert response.path == str(newer_dir / "index.html")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_preview_route.py -v`
Expected: FAIL with `AttributeError: module 'mission_control.server.app' has no attribute 'preview_file'`.

- [ ] **Step 3: Add the route**

Append to the end of `src/mission_control/server/app.py` (after `add_comment`):

```python
# Two routes, one function: FastAPI/Starlette's default redirect_slashes
# behavior would actually 307-redirect a bare "/preview" request to
# "/preview/" (query string preserved) and still resolve file_path="" —
# verified directly against this exact route shape before writing this
# plan — but registering the bare path explicitly avoids that extra
# round-trip and doesn't depend on a framework default this app doesn't
# otherwise rely on elsewhere.
@app.get("/api/missions/{mission_id}/preview")
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
    # path, so unlike every other route (which already trusts the local
    # caller for everything else — full file-write access is already
    # granted to agents), this one specifically stays inside the resolved
    # workspace directory no matter what file_path says.
    if not requested.is_relative_to(workspace_root):
        raise HTTPException(404, "not found")
    if not requested.is_file():
        raise HTTPException(404, "not found — does this project have an index.html?")

    return FileResponse(requested)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_preview_route.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest`
Expected: PASS (all previous tests still pass — this task only adds a new route, touches no existing route or function)

- [ ] **Step 6: Commit**

```bash
git add src/mission_control/server/app.py tests/test_preview_route.py
git commit -m "feat: add path-traversal-safe preview route serving a mission's workspace files"
```

---

### Task 2: Dashboard preview panel

**Files:**
- Modify: `src/mission_control/server/static/dashboard.html`

**Interfaces:**
- Consumes: Task 1's exact route shape — `GET /api/missions/{mission_id}/preview` (bare page) and `GET /api/missions/{mission_id}/preview/<relative-path>` (assets, resolved automatically by the browser against the iframe's own URL — no JS needed to rewrite asset paths).
- Produces: nothing consumed by a later task — this is the last task in the plan.

No automated test exists for this file (plain JS, no build step, no test runner configured in this project — same situation every prior dashboard-only task in this project has been in). Verify by extracting the `<script>` block and running `node --check` on it, then a manual check against a running server (Step 5 below).

- [ ] **Step 1: Add the panel markup**

In `src/mission_control/server/static/dashboard.html`, after the existing `#activityPanel` div and before `#detailGrid` (currently around lines 233-238 — if shifted, search for the `#activityPanel` div and insert right after its closing `</div>`), insert:

```html
  <div class="panel" id="previewPanel" style="margin-bottom:0.6rem;">
    <h2>Preview
      <span>
        <button type="button" id="previewReloadBtn">Reload</button>
        <a id="previewOpenLink" href="#" target="_blank" rel="noopener">Open in new tab ↗</a>
      </span>
    </h2>
    <iframe id="previewFrame"></iframe>
  </div>
```

- [ ] **Step 2: Add the CSS**

Add near the other panel-specific rules (e.g. after the `#activityList`/`#logList` block):

```css
  #previewPanel h2 span { display: flex; align-items: center; gap: 0.7rem; }
  #previewPanel h2 button { padding: 0.2rem 0.6rem; font-size: 0.85em; }
  #previewPanel h2 a { color: var(--dim); font-size: 0.85em; text-decoration: none; }
  #previewPanel h2 a:hover { color: var(--accent); }
  #previewFrame { width: 100%; height: 420px; border: 1px solid var(--border); border-radius: 3px; background: #fff; }
```

- [ ] **Step 3: Wire it up in `openMission`**

Find `function openMission(missionId)` (search for it — the plan's line numbers may have shifted from prior tasks' edits). Currently:

```javascript
function openMission(missionId) {
  state.currentMissionId = missionId;
  state.currentTaskId = null;
  document.getElementById("viewMissions").classList.add("hidden");
  document.getElementById("viewDetail").classList.remove("hidden");
  refreshDetail();
}
```

Change to (adds the two new lines that set the iframe/link URLs once per mission-open — deliberately not inside `refreshDetail()`, which runs on every 4-second poll, per the "no auto-reload" constraint):

```javascript
function openMission(missionId) {
  state.currentMissionId = missionId;
  state.currentTaskId = null;
  document.getElementById("viewMissions").classList.add("hidden");
  document.getElementById("viewDetail").classList.remove("hidden");
  setPreviewUrl(missionId);
  refreshDetail();
}

function setPreviewUrl(missionId) {
  const url = `/api/missions/${missionId}/preview?t=${Date.now()}`;
  document.getElementById("previewFrame").src = url;
  document.getElementById("previewOpenLink").href = `/api/missions/${missionId}/preview`;
}
```

(The `?t=${Date.now()}` cache-buster on the iframe's `src` only — not on the "open in new tab" link, which should just point at the clean URL — prevents the browser from serving a stale cached `index.html` after an agent edits the file. `fetchJSON` calls elsewhere in this file don't need this because JSON responses aren't typically cached the same way static HTML is by a `<iframe>` navigation.)

- [ ] **Step 4: Wire up the Reload button**

Add near the other `document.getElementById(...).addEventListener(...)` calls (e.g. near where `backBtn`'s listener is registered):

```javascript
document.getElementById("previewReloadBtn").addEventListener("click", () => {
  if (state.currentMissionId) setPreviewUrl(state.currentMissionId);
});
```

- [ ] **Step 5: Verify**

Extract and syntax-check the script block:

```bash
python -c "
import re
content = open('src/mission_control/server/static/dashboard.html', encoding='utf-8').read()
m = re.search(r'<script>(.*)</script>', content, re.S)
open('_dash_check.js', 'w', encoding='utf-8').write(m.group(1))
"
node --check _dash_check.js
```

Expected: no output (syntax OK). Delete `_dash_check.js` afterward.

Then, against a running server (restart it first so it picks up both tasks): open the mission that has `examples/tic-tac-toe/` as a task's workspace (or create a fresh ad-hoc task with `workspace_path` pointed at that folder), confirm the Preview panel renders the real page and the game is playable inside the iframe, click Reload after touching one of its files and confirm the change appears, and click "Open in new tab" and confirm it opens the same page standalone.

- [ ] **Step 6: Commit**

```bash
git add src/mission_control/server/static/dashboard.html
git commit -m "feat: add live preview panel to the dashboard"
```
