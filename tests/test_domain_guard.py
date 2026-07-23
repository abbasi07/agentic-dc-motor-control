"""Domain guard tests — deterministic, no OpenAI.

The guard is a conservative backstop: it must refuse clearly off-topic turns but
must never refuse legitimate control questions, numeric answers, or confirmations.
"""

from __future__ import annotations

import pytest

from agents.domain_guard import (
    classify_request,
    has_control_context,
    refusal_message,
    should_refuse,
)


@pytest.mark.parametrize(
    "text",
    [
        "write me a poem about the sea",
        "tell me a joke",
        "what's the weather today?",
        "give me a recipe for pasta",
        "who won the football game last night?",
        "translate hello into french",
        "what is the capital of France?",
    ],
)
def test_clear_off_topic_is_refused(text: str):
    assert should_refuse(text) is True
    assert classify_request(text) == "off_topic"


@pytest.mark.parametrize(
    "text",
    [
        "I have a DC motor with J=0.01, b=0.1, K=0.01, R=1, L=0.5",
        "design a PID controller for me",
        "what was the settling time on the step response?",
        "make it more robust to plant mismatch",
        "reduce the overshoot please",
        "I want it to reach 1.5 rad/s under 12 volts",
        "try an LQG with a Kalman filter",
    ],
)
def test_control_requests_are_allowed(text: str):
    assert should_refuse(text) is False
    assert classify_request(text) == "on_topic"


@pytest.mark.parametrize("text", ["yes", "looks good", "go ahead", "confirm", "no", "proceed"])
def test_confirmations_are_not_refused(text: str):
    assert should_refuse(text) is False


def test_numeric_answer_mid_session_is_allowed():
    # A terse numeric reply during an active session must not be refused.
    assert should_refuse("1.5 rad/s", in_progress=True) is False
    assert should_refuse("J=0.01, R=1", in_progress=True) is False


def test_symbols_count_as_control_context():
    assert has_control_context("set L to 0.5") is True


def test_refusal_message_steers_back_to_control():
    msg = refusal_message().lower()
    assert "control" in msg and "motor" in msg
