"""Domain guard: keep the copilot focused on DC-motor controller design.

The product is a *domain-locked* assistant. It designs controllers for DC motors
(later: arbitrary dynamic systems) and politely refuses everything else — recipes,
poems, general chit-chat, coding help unrelated to control, etc. — steering the user
back to the task.

Two layers enforce this:

  1. The LLM system prompt (see ``design_agent._system_prompt``) instructs the model
     to refuse off-topic requests. This handles subtle / conversational cases.
  2. This module is a cheap, deterministic *backstop* that can short-circuit an
     obviously off-topic turn before spending a model call, and is fully testable
     without OpenAI.

Design principle: **be conservative about refusing.** A false refusal (rejecting a
legitimate control question, a numeric answer, or a "looks good") is far worse than
letting an ambiguous message through to the LLM, which will ask a clarifying question
or refuse itself. So the deterministic layer only refuses messages that clearly match
an off-topic intent *and* carry no control-engineering context.
"""

from __future__ import annotations

import re

# --------------------------------------------------------------------------- #
# Vocabulary
# --------------------------------------------------------------------------- #
# Any of these terms present => the message is on-topic (control engineering).
CONTROL_TERMS: frozenset[str] = frozenset(
    {
        # domain nouns
        "motor", "dc motor", "servo", "actuator", "plant", "armature", "rotor",
        "controller", "control", "closed-loop", "closed loop", "open-loop", "open loop",
        # controller families
        "pid", "lqr", "lqg", "mpc", "mrac", "fuzzy", "robust", "adaptive",
        "kalman", "observer", "state feedback", "model predictive", "gain schedul",
        # gains / parameters
        "gain", "kp", "ki", "kd", "inertia", "damping", "friction", "resistance",
        "inductance", "torque", "back-emf", "back emf", "voltage", "current",
        # physical symbols (word-boundaried below)
        # performance metrics
        "settling", "overshoot", "rise time", "steady-state", "steady state",
        "tracking", "reference", "setpoint", "set point", "bandwidth", "pole",
        "transfer function", "step response", "saturation", "itae", "iae", "ise",
        # process verbs / nouns
        "spec", "requirement", "feasib", "tune", "tuning", "simulate", "simulation",
        "design", "certif", "export", "scorecard", "disturbance", "mismatch",
        "noise", "uncertainty", "stability", "stable", "unstable", "response",
        "speed", "velocity", "angular", "rad/s", "rpm", "omega",
    }
)

# Clear off-topic intents. Matched only when NO control term is present.
_OFF_TOPIC_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(poem|haiku|sonnet|limerick)\b",
        r"\bjokes?\b",
        r"\bweather\b",
        r"\b(recipe|cook|bake|cooking)\b",
        r"\b(stock|share)\s+price\b",
        r"\b(bitcoin|crypto(currency)?|ethereum)\b",
        r"\bpresident\b",
        r"\bcapital of\b",
        r"\btranslate\b",
        r"\bwrite\s+(me\s+)?(a|an)\s+(story|essay|song|email|poem|letter|blog)\b",
        r"\b(football|soccer|basketball|cricket|baseball)\b",
        r"\bhoroscope\b|\bzodiac\b",
        r"\b(movie|film|netflix|tv show)\b",
        r"\bwho\s+(is|was|won|are)\b",
        r"\bmeaning of life\b",
        r"\b(lyrics|song)\b",
    )
)

# Short confirmations / navigation — always allowed (they drive the negotiation).
_CONFIRMATIONAL: frozenset[str] = frozenset(
    {
        "yes", "y", "yeah", "yep", "yup", "no", "n", "nope", "ok", "okay", "k",
        "sure", "sounds good", "looks good", "go ahead", "proceed", "confirm",
        "confirmed", "agreed", "agree", "correct", "right", "that's right",
        "continue", "next", "done", "approve", "approved", "good", "great",
        "perfect", "cancel", "stop", "start over", "reset", "help",
    }
)

_NUMBER_RE = re.compile(r"[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?")
# Physical single-letter symbols used for motor params (word-boundaried).
_SYMBOL_RE = re.compile(r"(?<![A-Za-z])[JbKRL](?![A-Za-z])")

Verdict = str  # "on_topic" | "off_topic" | "ambiguous"


def _looks_confirmational(text: str) -> bool:
    t = text.strip().strip(".!").lower()
    return t in _CONFIRMATIONAL


def _looks_like_answer(text: str) -> bool:
    """A short, mostly-numeric reply (e.g. '1.5 rad/s', 'J=0.01, R=1')."""
    t = text.strip()
    if len(t) > 80:
        return False
    return bool(_NUMBER_RE.search(t)) or bool(_SYMBOL_RE.search(t))


def has_control_context(text: str) -> bool:
    t = text.lower()
    if any(term in t for term in CONTROL_TERMS):
        return True
    return bool(_SYMBOL_RE.search(text))


def classify_request(text: str, *, in_progress: bool = False) -> Verdict:
    """Classify a user turn as on-topic, off-topic, or ambiguous.

    ``in_progress`` indicates a motor/spec has already been defined this session, so
    terse replies are expected and should never be refused.
    """
    t = (text or "").strip()
    if not t:
        return "ambiguous"

    if has_control_context(t):
        return "on_topic"

    # Terse confirmations and numeric answers keep the negotiation moving.
    if _looks_confirmational(t) or (in_progress and _looks_like_answer(t)):
        return "ambiguous"

    if any(p.search(t) for p in _OFF_TOPIC_PATTERNS):
        return "off_topic"

    return "ambiguous"


def should_refuse(text: str, *, in_progress: bool = False) -> bool:
    """True only for clearly off-topic turns (the deterministic backstop)."""
    return classify_request(text, in_progress=in_progress) == "off_topic"


def refusal_message() -> str:
    return (
        "I'm a control-design copilot focused on DC-motor controllers, so I can't help "
        "with that. But I can help you model a DC motor, sanity-check performance "
        "requirements, or design, test, and certify a controller — all in simulation. "
        "What would you like to work on?"
    )


__all__ = [
    "CONTROL_TERMS",
    "classify_request",
    "has_control_context",
    "refusal_message",
    "should_refuse",
]
