"""Control Design Copilot — chat-first console (simulation only).

A single always-on conversation with the Design Copilot drives a deterministic
control-design engine. Alongside the chat, a structured panel lets you:

  * define ANY DC motor by its parameters (J, b, K, R, L, V_max) — presets only
    pre-fill the form as starting points,
  * set performance requirements (with or without the LLM),
  * pick a controller family explicitly (PID / Robust / LQR / LQG / MPC / MRAC /
    Fuzzy) or let the agent choose automatically, and
  * review scored results and export a certification package.

The simulator computes every metric; the assistant only plans and explains.

Run:
  uv run streamlit run saas/ui_streamlit.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import matplotlib.pyplot as plt
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.controller_registry import CONTROLLER_FAMILIES  # noqa: E402
from dc_motor.feasibility import check_feasibility  # noqa: E402
from dc_motor.motor_model import PARAM_UNITS  # noqa: E402
from dc_motor.plant import CTMS_PARAMS  # noqa: E402
from dc_motor.specs import ALLOWED_SCENARIOS, design_spec_from_dict  # noqa: E402
from experiments.ablation import (  # noqa: E402
    BENCHMARK_PROMPTS,
    run_ablation,
    summarize_ablation,
)
from saas import service  # noqa: E402
from saas.jobs import get_job_store  # noqa: E402
from saas.present import (  # noqa: E402
    DESIGN_STRATEGY,
    constraints_as_lines,
    controller_name,
    job_status_label,
    limits_summary,
    metric_name,
    scenario_name,
    scenarios_as_lines,
    scorecard_rows,
    session_outcome,
)

st.set_page_config(
    page_title="Control Design Copilot",
    page_icon="◎",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
<style>
@import url('https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600;700&display=swap');
html, body, [class*="css"] { font-family: "IBM Plex Sans", "Segoe UI", sans-serif; }
.stApp {
  background:
    radial-gradient(900px 420px at 0% -5%, #d7ebe6 0%, transparent 50%),
    linear-gradient(180deg, #eef2f4 0%, #e7ecf0 100%);
}
[data-testid="stSidebar"] { background: #13232b; border-right: 1px solid #1f3640; }
[data-testid="stSidebar"] h1,
[data-testid="stSidebar"] h2,
[data-testid="stSidebar"] h3,
[data-testid="stSidebar"] label,
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] .stMarkdown,
[data-testid="stSidebar"] [data-testid="stCaptionContainer"],
[data-testid="stSidebar"] [data-testid="stExpander"] summary {
  color: #dbe6ea !important;
}
[data-testid="stSidebar"] [data-baseweb="select"] * { color: #10232b !important; }
[data-testid="stSidebar"] [data-testid="stSlider"] * { color: #dbe6ea !important; }

.cdc-brand { font-size: 1.55rem; font-weight: 700; color: #102028; margin: 0; }
.cdc-sub { color: #5a6d76; margin: 0.2rem 0 0.7rem 0; }
.cdc-pill {
  display: inline-block; padding: 0.22rem 0.7rem; border-radius: 999px;
  background: #edf2f4; color: #314650; border: 1px solid #c5d0d6;
  font-size: 0.8rem; margin: 0 0.35rem 0.4rem 0;
}
.cdc-pill.on { background: #e8f6f1; color: #0f5c48; border-color: #9ed4c2; }
.cdc-pill.off { background: #fdeceb; color: #a23b32; border-color: #eab8b2; }
.cdc-panelhead { font-size: 1.05rem; font-weight: 600; color: #102028; margin: 0.2rem 0 0.4rem 0; }
.stButton > button[kind="primary"] {
  background: #0f766e !important; border-color: #0f766e !important;
}
[data-testid="stChatMessage"] { background: rgba(255,255,255,0.85); border-radius: 12px; }
</style>
""",
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #
# Starting-point motor presets (they only PRE-FILL the custom form; the plant
# actually simulated is always the numbers you submit).
MOTOR_PRESETS: dict[str, dict[str, float]] = {
    "CTMS benchmark motor": {
        "J": CTMS_PARAMS.J, "b": CTMS_PARAMS.b, "K": CTMS_PARAMS.K,
        "R": CTMS_PARAMS.R, "L": CTMS_PARAMS.L, "V_max": 12.0,
    },
    "Small hobby DC motor": {
        "J": 1.0e-5, "b": 1.0e-5, "K": 0.02, "R": 2.0, "L": 1.0e-3, "V_max": 6.0,
    },
    "Geared gearmotor (24 V)": {
        "J": 5.0e-4, "b": 1.0e-3, "K": 0.05, "R": 3.0, "L": 1.5e-3, "V_max": 24.0,
    },
}
_DEFAULT_PRESET = "CTMS benchmark motor"

SAMPLE_GOALS = [
    "Settle under 1.5 s, overshoot under 10%, keep |voltage| ≤ 12 V, prefer low control effort.",
    "Settle under 1.2 s, overshoot under 8%, reject a small load (~0.01 N·m) after 1.5 s, |voltage| ≤ 12 V.",
    "Tight step response, overshoot under 5%, prioritize robustness to plant parameter mismatch and sensor noise.",
]

QUICK_FIXES = [
    ("Retune gains", "Please retune the controller."),
    ("Relax settling", "Relax the settling-time requirement to 2.5 s."),
    ("Add load test", "Add a load disturbance test."),
    ("More robust", "Make the design more robust to plant mismatch."),
]

# Common hard-constraint metrics offered in the structured requirements form.
SPEC_METRICS = [
    ("settling_time_s", "Settling time ≤", "s", 1.5, 0.05, 15.0, 0.1),
    ("overshoot_pct", "Overshoot ≤", "%", 10.0, 0.0, 100.0, 1.0),
    ("steady_state_error", "Steady-state error ≤", "", 0.05, 0.001, 1.0, 0.01),
]

# Controller family options for explicit selection ("auto" first).
_FAMILY_LABELS: dict[str, str] = {"auto": "Auto — agent decides (recommended)"}
for _fam in CONTROLLER_FAMILIES:
    _FAMILY_LABELS[_fam.type_name] = _fam.label
_FAMILY_OPTIONS = list(_FAMILY_LABELS.keys())
_FAMILY_DESC = {fam.type_name: fam.description for fam in CONTROLLER_FAMILIES}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _has_openai() -> bool:
    key = os.getenv("OPENAI_API_KEY", "")
    return bool(key) and not key.startswith("sk-your-key")


def _init_state() -> None:
    st.session_state.setdefault("mode", "heuristic")
    st.session_state.setdefault("max_iter", 5)
    st.session_state.setdefault("preset_choice", _DEFAULT_PRESET)
    preset = MOTOR_PRESETS[_DEFAULT_PRESET]
    for k in ("J", "b", "K", "R", "L", "V_max"):
        st.session_state.setdefault(f"m_{k}", float(preset[k]))
    st.session_state.setdefault("m_name", "my_dc_motor")


def _ensure_job():
    if "job_id" not in st.session_state:
        job = service.create_job(mode=st.session_state.get("mode", "heuristic"))
        job.max_iterations = st.session_state.get("max_iter", 5)
        st.session_state.job_id = job.job_id
    return get_job_store().get(st.session_state.job_id)


def _load_preset() -> None:
    preset = MOTOR_PRESETS[st.session_state["preset_choice"]]
    for k in ("J", "b", "K", "R", "L", "V_max"):
        st.session_state[f"m_{k}"] = float(preset[k])


def _motor_is_set(job) -> bool:
    return job.motor_dict is not None or job._motor is not None


def _spec_is_set(job) -> bool:
    return job.spec_dict is not None or job._spec is not None


def _has_results(job) -> bool:
    return bool(job.scorecard)


def _apply_motor_form(job) -> None:
    payload = {
        "J": float(st.session_state["m_J"]),
        "b": float(st.session_state["m_b"]),
        "K": float(st.session_state["m_K"]),
        "R": float(st.session_state["m_R"]),
        "L": float(st.session_state["m_L"]),
        "V_max": float(st.session_state["m_V_max"]),
        "name": st.session_state.get("m_name") or "my_dc_motor",
    }
    service.set_motor_from_params(job, payload)


def _apply_spec_form(job, omega_ref, v_max, t_final, cons, scenarios, low_effort) -> None:
    """Build a DesignSpec from the structured form (no LLM) and store it."""
    hard = {m: {"op": "<=", "limit": float(v)} for m, v in cons.items()}
    prefs = {"ITAE": 1.0, "overshoot_pct": 0.05, "control_effort": 0.2 if low_effort else 0.01}
    raw = (
        f"Structured requirements: omega_ref={omega_ref} rad/s, |V|<={v_max} V, "
        f"t_final={t_final} s, constraints="
        + ", ".join(f"{m}<= {v}" for m, v in cons.items())
        + f"; scenarios={', '.join(scenarios) or 'step_1rads'}."
    )
    data = {
        "raw_spec": raw,
        "hard_constraints": hard,
        "soft_preferences": prefs,
        "required_scenarios": scenarios or ["step_1rads"],
        "omega_ref": float(omega_ref),
        "V_max": float(v_max),
        "V_min": -float(v_max),
        "t_final": float(t_final),
    }
    spec = design_spec_from_dict(data, raw_spec=raw, source="manual")
    job._spec = spec
    job.spec_dict = spec.to_dict()
    job.nl_spec = raw
    job.confirmed = False
    # Physics feasibility against the current (custom or default) motor.
    params = service.effective_motor_params(job)
    job.feasibility = check_feasibility(params, spec).to_dict()
    job.status = "spec_ready"
    job.touch()


def _run_design(job, controller_type: str) -> None:
    if not _spec_is_set(job):
        raise RuntimeError("Set the performance requirements first (Requirements tab).")
    if controller_type == "auto":
        job.mode = st.session_state.get("mode", "heuristic")
        service.confirm_and_run(job, max_iterations=st.session_state.get("max_iter", 5))
    else:
        session = service.get_agent_session(job)
        session.design_controller(controller_type=controller_type)


def _send_chat(job, text: str) -> None:
    with st.spinner("Copilot is working…"):
        service.agent_chat(job, text)


def _plot_scorecard(scorecard: dict) -> None:
    if not scorecard or "scenarios" not in scorecard:
        return
    fig, axes = plt.subplots(2, 1, figsize=(7.2, 4.8), sharex=True)
    fig.patch.set_facecolor("#fbfcfd")
    plotted = False
    for ax in axes:
        ax.set_facecolor("#fbfcfd")
        ax.grid(True, alpha=0.28, color="#9aaeb8")
    for item in scorecard["scenarios"]:
        tr = item.get("trajectories")
        if not tr:
            continue
        plotted = True
        axes[0].plot(tr["t"], tr["omega"], label=item["name"], linewidth=1.6)
        axes[1].plot(tr["t"], tr["u"], label=item["name"], linewidth=1.6)
    if not plotted:
        plt.close(fig)
        return
    axes[0].set_ylabel("Plant output")
    axes[0].legend(fontsize=8, frameon=False)
    axes[0].set_title("Closed-loop response (from simulation)", fontsize=10, loc="left")
    axes[1].set_ylabel("Control input u")
    axes[1].set_xlabel("Time [s]")
    axes[1].legend(fontsize=8, frameon=False)
    st.pyplot(fig, use_container_width=True)
    plt.close(fig)


def _render_controller(best: dict) -> None:
    params = best.get("params") or {}
    st.write(f"**{controller_name(best.get('kind'))}**")
    mapping = {"Kp": "Proportional Kp", "Ki": "Integral Ki", "Kd": "Derivative Kd", "N": "Deriv. filter N"}
    nice = [(label, params[key]) for key, label in mapping.items() if key in params]
    if nice:
        cols = st.columns(len(nice))
        for i, (label, val) in enumerate(nice):
            with cols[i]:
                shown = f"{float(val):.4g}" if isinstance(val, (int, float)) else str(val)
                st.metric(label, shown)
    others = {k: v for k, v in params.items() if k not in mapping}
    if others:
        with st.expander("Other controller settings"):
            for k, v in others.items():
                st.write(f"**{k}:** {v}")


# --------------------------------------------------------------------------- #
# Sidebar — session setup
# --------------------------------------------------------------------------- #
_init_state()

with st.sidebar:
    st.markdown("### Session setup")
    st.caption("The plant is the DC motor you define on the right. These control the redesign loop.")

    mode_keys = list(DESIGN_STRATEGY.keys())
    mode = st.selectbox(
        "Design strategy (for Auto)",
        options=mode_keys,
        index=mode_keys.index(st.session_state.get("mode", "heuristic")),
        format_func=lambda m: DESIGN_STRATEGY[m]["label"],
    )
    st.caption(DESIGN_STRATEGY[mode]["help"])
    st.session_state.mode = mode

    max_iter = st.slider(
        "Max redesign attempts", 1, 12, int(st.session_state.get("max_iter", 5)),
        help="How many tune / redesign tries the Auto orchestrator may use.",
    )
    st.session_state.max_iter = max_iter

    if _has_openai():
        st.caption("🟢 OpenAI key detected — natural-language chat is enabled.")
    else:
        st.caption(
            "🟡 No OpenAI key — chat is disabled, but you can still define the motor, "
            "set requirements, and design controllers with the structured panel."
        )

    if st.button("Start over", use_container_width=True, type="primary"):
        job = service.create_job(mode=mode)
        job.max_iterations = max_iter
        st.session_state.job_id = job.job_id
        st.rerun()

    st.divider()
    with st.expander("Advanced · research ablation"):
        st.caption("Compare design strategies on the same prompts (research use).")
        subset_ids = st.multiselect(
            "Prompt set",
            options=[p["id"] for p in BENCHMARK_PROMPTS],
            default=["easy_step", "load_feasible", "load_infeasible_settle", "mismatch"],
        )
        ab_modes = st.multiselect(
            "Strategies", options=mode_keys, default=["script", "heuristic"],
            format_func=lambda m: DESIGN_STRATEGY[m]["label"],
        )
        if st.button("Run ablation"):
            prompts = [p for p in BENCHMARK_PROMPTS if p["id"] in set(subset_ids)]
            try:
                with st.spinner("Running ablation…"):
                    rows = run_ablation(modes=ab_modes, prompts=prompts, max_iterations=4, maxiter_scipy=5)
                st.json(summarize_ablation(rows))
                st.dataframe([r.to_dict() for r in rows], use_container_width=True, hide_index=True)
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))


job = _ensure_job()
job.mode = st.session_state.mode
job.max_iterations = st.session_state.max_iter

# --------------------------------------------------------------------------- #
# Header + progress pills
# --------------------------------------------------------------------------- #
st.markdown('<p class="cdc-brand">Control Design Copilot</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="cdc-sub">Define <b>any</b> DC motor, state your goals in plain English, and let the '
    "copilot design & certify a controller — simulation only. The simulator computes every metric.</p>",
    unsafe_allow_html=True,
)


def _pill(label: str, on: bool) -> str:
    cls = "on" if on else "off"
    mark = "✓" if on else "•"
    return f'<span class="cdc-pill {cls}">{mark} {label}</span>'


st.markdown(
    _pill("Motor defined", _motor_is_set(job))
    + _pill("Requirements set", _spec_is_set(job))
    + _pill("Controller designed", _has_results(job))
    + f'<span class="cdc-pill">{job_status_label(job.status)}</span>',
    unsafe_allow_html=True,
)

col_chat, col_panel = st.columns([1.15, 1.0], gap="large")

# =========================== LEFT: Conversation =========================== #
with col_chat:
    st.markdown('<p class="cdc-panelhead">💬 Conversation</p>', unsafe_allow_html=True)
    chat_box = st.container(height=520)
    with chat_box:
        if not job.chat:
            st.chat_message("assistant").markdown(
                "Hi! I'm your Control Design Copilot. Tell me about your DC motor and what you "
                "want the closed loop to do — or use the panel on the right to enter numbers "
                "directly. For example: *“I have a 12 V motor, J≈0.01, and I want it to settle "
                "under 1.5 s with less than 10% overshoot.”*"
            )
        for msg in job.chat:
            role = "user" if msg.get("role") == "user" else "assistant"
            st.chat_message(role).markdown(msg.get("content", ""))

    if _has_openai():
        with st.form("chat_form", clear_on_submit=True):
            user_text = st.text_area(
                "Message the copilot",
                placeholder="Describe your motor, state your goals, or ask “what was the settling time?”",
                height=80,
                label_visibility="collapsed",
            )
            sent = st.form_submit_button("Send", type="primary", use_container_width=True)
        if sent and user_text.strip():
            try:
                _send_chat(job, user_text.strip())
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))
        st.caption("Try: “Define J=0.01, b=0.1, K=0.01, R=1, L=0.5, 12 V” · “Design an LQG” · “What was the overshoot?”")
    else:
        st.info(
            "Chat needs an `OPENAI_API_KEY`. Without it, use the structured panel on the right — "
            "it drives the same deterministic engine."
        )

# ======================== RIGHT: Structured panel ======================== #
with col_panel:
    tab_motor, tab_req, tab_design, tab_results = st.tabs(
        ["① Motor", "② Requirements", "③ Design", "④ Results & export"]
    )

    # ---- ① Motor -------------------------------------------------------- #
    with tab_motor:
        st.markdown("**Define your DC motor**")
        st.caption("Enter the physical parameters of the motor to control. Presets only pre-fill the form.")

        pcols = st.columns([2, 1])
        with pcols[0]:
            st.selectbox("Start from a preset", options=list(MOTOR_PRESETS.keys()), key="preset_choice")
        with pcols[1]:
            st.write("")
            st.button("Load into form", on_click=_load_preset, use_container_width=True)

        c1, c2 = st.columns(2)
        with c1:
            st.number_input(f"J — rotor inertia [{PARAM_UNITS['J']}]", key="m_J", format="%.6g", step=0.001)
            st.number_input(f"K — torque/back-emf [{PARAM_UNITS['K']}]", key="m_K", format="%.6g", step=0.001)
            st.number_input(f"L — inductance [{PARAM_UNITS['L']}]", key="m_L", format="%.6g", step=0.001)
        with c2:
            st.number_input(f"b — viscous friction [{PARAM_UNITS['b']}]", key="m_b", format="%.6g", step=0.001)
            st.number_input(f"R — resistance [{PARAM_UNITS['R']}]", key="m_R", format="%.6g", step=0.1)
            st.number_input("V_max — voltage budget [V]", key="m_V_max", format="%.6g", step=1.0)
        st.text_input("Motor name", key="m_name")

        if st.button("Set this motor", type="primary", use_container_width=True):
            try:
                _apply_motor_form(job)
                st.success("Motor set as the plant.")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))

        if job.motor_dict:
            ch = job.motor_dict.get("characteristics", {})
            st.markdown("**Derived open-loop characteristics** (from the transfer function)")
            mc1, mc2, mc3 = st.columns(3)
            mc1.metric("No-load speed", f"{ch.get('omega_max_rad_s', float('nan')):.3g} rad/s")
            mc2.metric("Mech. time const", f"{ch.get('tau_mech_s', float('nan')):.3g} s")
            mc3.metric("Damping", str(ch.get("damping", "—")))
            for w in job.motor_dict.get("warnings", []):
                st.warning(w)

    # ---- ② Requirements ------------------------------------------------- #
    with tab_req:
        st.markdown("**Performance requirements**")
        st.caption("Set the hard limits and tests to run. These feed the deterministic evaluator.")

        rc1, rc2, rc3 = st.columns(3)
        omega_ref = rc1.number_input("Speed reference [rad/s]", value=1.0, min_value=0.01, max_value=20.0, step=0.5)
        default_v = float(st.session_state.get("m_V_max", 12.0))
        v_max = rc2.number_input("Voltage limit |V| [V]", value=default_v, min_value=1.0, max_value=48.0, step=1.0)
        t_final = rc3.number_input("Sim horizon [s]", value=3.0, min_value=0.5, max_value=30.0, step=0.5)

        cons: dict[str, float] = {}
        st.markdown("**Hard limits**")
        for metric, label, unit, default, lo, hi, step in SPEC_METRICS:
            lc, vc = st.columns([1, 1])
            use = lc.checkbox(f"{label} ({unit})" if unit else label, value=True, key=f"use_{metric}")
            val = vc.number_input(
                metric, value=float(default), min_value=float(lo), max_value=float(hi),
                step=float(step), key=f"val_{metric}", label_visibility="collapsed",
            )
            if use:
                cons[metric] = val

        scenarios = st.multiselect(
            "Simulation tests to run",
            options=sorted(ALLOWED_SCENARIOS),
            default=["step_1rads"],
            format_func=scenario_name,
        )
        low_effort = st.checkbox("Prefer low control effort", value=True)

        if st.button("Set requirements", type="primary", use_container_width=True):
            try:
                _apply_spec_form(job, omega_ref, v_max, t_final, cons, scenarios, low_effort)
                st.success("Requirements set.")
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))

        if _has_openai():
            st.caption("Prefer words? Describe goals in the chat and the copilot will build these for you.")

        if job.spec_dict:
            st.divider()
            st.markdown("**Current requirements**")
            for line in constraints_as_lines(job.spec_dict):
                st.markdown(f"- {line}")
            st.markdown("**Tests:** " + ", ".join(scenarios_as_lines(job.spec_dict)))
            for k, v in limits_summary(job.spec_dict).items():
                st.caption(f"{k}: {v}")
            if job.feasibility:
                if job.feasibility.get("feasible", True):
                    st.success("Physics check: these targets look achievable on this motor.")
                else:
                    st.error("Physics check: not achievable as stated — see notes below.")
                for iss in job.feasibility.get("issues", []):
                    if iss.get("severity") in {"error", "warning"}:
                        st.warning(f"{iss.get('message', '')} {iss.get('suggestion', '')}".strip())

    # ---- ③ Design ------------------------------------------------------- #
    with tab_design:
        st.markdown("**Choose a controller**")
        st.caption("Let the agent decide, or force a specific control family. Each choice re-runs every test.")

        choice = st.radio(
            "Controller family",
            options=_FAMILY_OPTIONS,
            format_func=lambda t: _FAMILY_LABELS[t],
            label_visibility="collapsed",
        )
        if choice == "auto":
            st.caption(
                f"The **{DESIGN_STRATEGY[st.session_state.mode]['label']}** orchestrator will pick and, "
                "if a test fails, switch family or retune automatically (up to your attempt budget)."
            )
        else:
            st.caption(_FAMILY_DESC.get(choice, ""))

        disabled = not _spec_is_set(job)
        if disabled:
            st.info("Set the requirements first (tab ②).")
        if st.button("Design controller", type="primary", use_container_width=True, disabled=disabled):
            try:
                with st.spinner("Designing and running all simulation tests…"):
                    _run_design(job, choice)
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))

        if _has_results(job):
            st.divider()
            st.markdown("**Quick changes** (each re-runs the tests)")
            qcols = st.columns(2)
            for i, (label, phrase) in enumerate(QUICK_FIXES):
                with qcols[i % 2]:
                    if st.button(label, key=f"qf_{i}", use_container_width=True):
                        try:
                            with st.spinner("Updating the design…"):
                                service.apply_feedback_and_maybe_rerun(job, phrase, use_llm=False, rerun=True)
                            st.rerun()
                        except Exception as exc:  # noqa: BLE001
                            st.error(str(exc))

    # ---- ④ Results & export -------------------------------------------- #
    with tab_results:
        best = (job.session_dict or {}).get("best") or {}
        all_pass = bool(best.get("all_pass"))
        kind = best.get("kind")
        sess_status = (job.session_dict or {}).get("status")

        if not job.session_dict and not job.scorecard:
            st.info("No results yet. Define a motor, set requirements, then design a controller.")
        else:
            if all_pass:
                st.success(f"**Requirements met.** {controller_name(kind)} passed every required test.")
            elif job.session_dict:
                st.error(f"**Not fully met.** Best: {controller_name(kind)}. {session_outcome(sess_status)}")
            elif job.error:
                st.error(job.error)

            if job.certification:
                if job.certification.get("allowed"):
                    st.caption("✅ Certification gate: eligible to export a simulation package.")
                else:
                    st.caption(f"⛔ Certification gate: blocked — {job.certification.get('reason', 'requirements not met.')}")

            if best:
                _render_controller(best)

            if job.scorecard:
                rows = scorecard_rows(job.scorecard)
                if rows:
                    st.dataframe(rows, use_container_width=True, hide_index=True)
                _plot_scorecard(job.scorecard)
                with st.expander("Measured numbers & what the designer tried", expanded=False):
                    for item in job.scorecard.get("scenarios", []):
                        metrics = item.get("metrics") or {}
                        st.markdown(f"**{scenario_name(item.get('name', 'test'))}**")
                        show = []
                        for key in ("settling_time_s", "overshoot_pct", "steady_state_error", "control_effort", "recovery_time_s"):
                            if key in metrics:
                                val = metrics[key]
                                try:
                                    show.append(f"{metric_name(key)}: `{float(val):.4g}`")
                                except (TypeError, ValueError):
                                    show.append(f"{metric_name(key)}: `{val}`")
                        st.write(" · ".join(show) if show else "—")
                    trace = (job.session_dict or {}).get("action_trace") or []
                    if trace:
                        st.markdown("**Redesign attempts**")
                        for a in trace:
                            act = str(a.get("action", "")).replace("_", " ")
                            mark = "✓" if a.get("all_pass") else "✗"
                            st.markdown(f"- Attempt {a.get('iteration')}: {act} ({mark})")
                    if (job.session_dict or {}).get("rationale"):
                        st.markdown("**Design rationale**")
                        st.write(job.session_dict["rationale"])

            st.divider()
            st.markdown("**Export**")
            st.caption("Download a certification package (controller + scorecard + rationale).")
            if st.button("Download certification package", use_container_width=True):
                try:
                    path = service.export_job(job)
                    st.success(f"Saved to `{path}`")
                except Exception as exc:  # noqa: BLE001
                    st.error(str(exc))
