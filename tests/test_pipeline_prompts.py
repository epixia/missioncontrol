"""Pure-function tests for prompt composition and result extraction — no
DB, no adapters, no network. These are the building blocks Task 3/4 wire
into the live pipeline."""

from mission_control.server.pipeline_prompts import (
    build_coder_prompt,
    build_orchestrator_prompt,
    build_reviewer_prompt,
    extract_claude_code_result_text,
)


def test_build_orchestrator_prompt_includes_goal():
    prompt = build_orchestrator_prompt("Add a health check endpoint")
    assert "Add a health check endpoint" in prompt
    assert "Orchestrator" in prompt


def test_build_coder_prompt_includes_goal_and_orchestrator_output():
    prompt = build_coder_prompt("Add a health check endpoint", "Plan: add GET /healthz")
    assert "Add a health check endpoint" in prompt
    assert "Plan: add GET /healthz" in prompt
    assert "Coder" in prompt


def test_build_reviewer_prompt_includes_goal_and_coder_output():
    prompt = build_reviewer_prompt("Add a health check endpoint", "Added GET /healthz returning 200")
    assert "Add a health check endpoint" in prompt
    assert "Added GET /healthz returning 200" in prompt
    assert "Reviewer" in prompt


def test_extract_claude_code_result_text_from_result_event():
    assert extract_claude_code_result_text("result", {"result": "OK", "is_error": False}) == "OK"


def test_extract_claude_code_result_text_ignores_non_result_events():
    assert extract_claude_code_result_text("assistant", {"result": "should not matter"}) is None


def test_extract_claude_code_result_text_handles_missing_field():
    assert extract_claude_code_result_text("result", {"is_error": True}) is None


def test_extract_claude_code_result_text_handles_empty_string():
    assert extract_claude_code_result_text("result", {"result": ""}) is None
