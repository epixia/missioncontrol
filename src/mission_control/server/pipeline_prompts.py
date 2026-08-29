"""Pure functions for the agent pipeline: persona prompts, prompt
composition, and Claude Code result-text extraction. No I/O, no DB, no
adapter calls — kept separate from app.py's orchestration so these are
unit-testable without a running server or live API calls.

See docs/superpowers/specs/2026-08-27-agent-pipeline-design.md.
"""

from __future__ import annotations

from typing import Any

ORCHESTRATOR_PROMPT = (
    "You are the Orchestrator agent in a three-agent pipeline "
    "(Orchestrator -> Coder -> Reviewer). You do not write code yourself. "
    "Read the goal below and produce a concise, actionable plan for the "
    "Coder agent to follow. Be specific about what files or areas of the "
    "codebase are likely involved and what \"done\" looks like.\n\nGoal:\n"
)

CODER_PROMPT = (
    "You are the Coder agent in a three-agent pipeline "
    "(Orchestrator -> Coder -> Reviewer). The Orchestrator has produced a "
    "plan for the goal below. Implement it against the current workspace. "
    "When you finish, concisely report what you changed and why.\n\nGoal:\n"
)

REVIEWER_PROMPT = (
    "You are the Reviewer agent in a three-agent pipeline "
    "(Orchestrator -> Coder -> Reviewer). The Coder has reported the work "
    "below in response to the original goal. Critique it against the goal: "
    "does it actually achieve it? Note anything wrong, missing, or risky. "
    "End with a clear verdict: either \"Approved\" or \"Concerns:\" "
    "followed by specifics.\n\nGoal:\n"
)


# The kanban block is appended AFTER the goal, and the dashboard's
# extractGoal() (static/dashboard.html) recovers the raw goal from a stored
# orchestrator prompt by slicing from "Goal:\n" onward — so it has to know
# where the goal stops. Both sides split on this exact string; change it in
# one place and you must change it in the other (guarded by
# tests/test_pipeline_prompts.py).
KANBAN_MARKER = "\n\nYou have a shared kanban board"


_ORCHESTRATOR_KANBAN_DIRECTIVE = (
    "This is required, not optional: before you finish, create one ticket "
    "in the \"todo\" column for each distinct step in your plan, so the "
    "Coder and the user can see the breakdown."
)

_CODER_KANBAN_DIRECTIVE = (
    "This is required, not optional: move each ticket from the "
    "Orchestrator's plan to \"doing\" when you start that step and to "
    "\"done\" when you finish it. If no tickets exist yet, create one per "
    "step first, then work through them the same way."
)

_REVIEWER_KANBAN_DIRECTIVE = (
    "This is required, not optional: leave at least one comment on the "
    "board summarizing your verdict before you finish — comment on an "
    "existing ticket if one fits, or create one if none exist."
)


def kanban_instructions(mission_id: str, directive: str) -> str:
    base = f"http://127.0.0.1:8420/api/missions/{mission_id}/tickets"
    return (
        f"{KANBAN_MARKER} for this mission, visible to "
        "the user and to the other agents in this pipeline. You have "
        "network access to Mission Control's own local API via curl:\n"
        f"- Create a ticket: curl -s -X POST '{base}?author_role=<your-role>' "
        "-H 'Content-Type: application/json' "
        '-d \'{"title": "...", "description": "..."}\' '
        "(the response JSON includes the new ticket's \"id\")\n"
        f"- List tickets (to find one another agent created): curl -s '{base}'\n"
        "- Move a ticket (column is one of backlog/todo/doing/done): "
        "curl -s -X PATCH 'http://127.0.0.1:8420/api/tickets/<ticket_id>' "
        "-H 'Content-Type: application/json' -d '{\"column\": \"doing\"}'\n"
        "- Comment on a ticket: "
        "curl -s -X POST "
        "'http://127.0.0.1:8420/api/tickets/<ticket_id>/comments?author_role=<your-role>' "
        "-H 'Content-Type: application/json' -d '{\"text\": \"...\"}'\n"
        f"{directive}"
    )


def build_orchestrator_prompt(goal: str, mission_id: str) -> str:
    return f"{ORCHESTRATOR_PROMPT}{goal}{kanban_instructions(mission_id, _ORCHESTRATOR_KANBAN_DIRECTIVE)}"


def build_coder_prompt(goal: str, orchestrator_result: str, mission_id: str) -> str:
    return (
        f"{CODER_PROMPT}{goal}\n\nOrchestrator's plan:\n{orchestrator_result}"
        f"{kanban_instructions(mission_id, _CODER_KANBAN_DIRECTIVE)}"
    )


def build_reviewer_prompt(goal: str, coder_result: str, mission_id: str) -> str:
    return (
        f"{REVIEWER_PROMPT}{goal}\n\nCoder's work:\n{coder_result}"
        f"{kanban_instructions(mission_id, _REVIEWER_KANBAN_DIRECTIVE)}"
    )


def extract_claude_code_result_text(event_type: str, payload: dict[str, Any]) -> str | None:
    """Given one already-stored TaskEvent's (event_type, payload), return
    the final plain-text result if this is Claude Code's terminal `result`
    event, else None. Callers should call this for every event in a task's
    stream and keep the last non-None value — only the `result` event ever
    returns non-None, so the "last" one is also the only one."""
    if event_type != "result":
        return None
    if payload.get("is_error"):
        return None
    result = payload.get("result")
    return result if isinstance(result, str) and result else None
