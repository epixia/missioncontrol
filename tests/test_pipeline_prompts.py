"""Pure-function tests for prompt composition and result extraction — no
DB, no adapters, no network. These are the building blocks the pipeline
wires into the live pipeline."""

from mission_control.server.pipeline_prompts import (
    KANBAN_MARKER,
    build_coder_prompt,
    build_orchestrator_prompt,
    build_reviewer_prompt,
    extract_claude_code_result_text,
)


def _extract_goal(prompt: str) -> str:
    """Mirror of dashboard.html's extractGoal() — the Agents panel recovers
    the raw goal from a stored orchestrator prompt this way. Kept in sync by
    the tests below."""
    marker = "Goal:\n"
    idx = prompt.find(marker)
    rest = prompt[idx + len(marker) :] if idx >= 0 else prompt
    return rest.split(KANBAN_MARKER)[0]


def test_build_orchestrator_prompt_includes_goal():
    prompt = build_orchestrator_prompt("Add a health check endpoint", "mission-1")
    assert "Add a health check endpoint" in prompt
    assert "Orchestrator" in prompt


def test_build_coder_prompt_includes_goal_and_orchestrator_output():
    prompt = build_coder_prompt("Add a health check endpoint", "Plan: add GET /healthz", "mission-1")
    assert "Add a health check endpoint" in prompt
    assert "Plan: add GET /healthz" in prompt
    assert "Coder" in prompt


def test_build_reviewer_prompt_includes_goal_and_coder_output():
    prompt = build_reviewer_prompt("Add a health check endpoint", "Added GET /healthz returning 200", "mission-1")
    assert "Add a health check endpoint" in prompt
    assert "Added GET /healthz returning 200" in prompt
    assert "Reviewer" in prompt


def test_build_orchestrator_prompt_includes_kanban_instructions_for_its_mission():
    prompt = build_orchestrator_prompt("Add a health check endpoint", "mission-42")
    assert "/api/missions/mission-42/tickets" in prompt


def test_build_coder_prompt_includes_kanban_instructions_for_its_mission():
    prompt = build_coder_prompt("goal", "plan", "mission-42")
    assert "/api/missions/mission-42/tickets" in prompt


def test_build_reviewer_prompt_includes_kanban_instructions_for_its_mission():
    prompt = build_reviewer_prompt("goal", "work", "mission-42")
    assert "/api/missions/mission-42/tickets" in prompt


def test_kanban_block_starts_with_the_marker_the_dashboard_splits_on():
    """dashboard.html's extractGoal() truncates the stored prompt at
    KANBAN_MARKER. If the kanban paragraph's opening words ever change
    without updating both sides, the Agents panel silently starts rendering
    the whole kanban boilerplate in every pipeline run header."""
    prompt = build_orchestrator_prompt("goal", "m-1")
    assert prompt.count(KANBAN_MARKER) == 1


def test_extract_goal_recovers_only_the_goal_from_a_pipeline_prompt():
    assert _extract_goal(build_orchestrator_prompt("Add a health check endpoint", "m-1")) == (
        "Add a health check endpoint"
    )
    assert _extract_goal(build_coder_prompt("Add a health check endpoint", "the plan", "m-1")) == (
        "Add a health check endpoint\n\nOrchestrator's plan:\nthe plan"
    )
    assert _extract_goal(build_reviewer_prompt("Add a health check endpoint", "the work", "m-1")) == (
        "Add a health check endpoint\n\nCoder's work:\nthe work"
    )


def test_extract_claude_code_result_text_from_result_event():
    assert extract_claude_code_result_text("result", {"result": "OK", "is_error": False}) == "OK"


def test_extract_claude_code_result_text_ignores_non_result_events():
    assert extract_claude_code_result_text("assistant", {"result": "should not matter"}) is None


def test_extract_claude_code_result_text_handles_missing_field():
    assert extract_claude_code_result_text("result", {"is_error": True}) is None


def test_extract_claude_code_result_text_handles_empty_string():
    assert extract_claude_code_result_text("result", {"result": ""}) is None


def test_extract_claude_code_result_text_treats_is_error_result_as_failure():
    """An error_max_turns-style result can still carry a `result` string and
    exit 0 — is_error: true must still mean 'no result_text', per the spec's
    'on failure, result_text stays None'."""
    assert extract_claude_code_result_text(
        "result", {"type": "result", "is_error": True, "result": "Reached max turns"}
    ) is None
