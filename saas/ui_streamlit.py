"""Control Design Copilot — a guided 3-step console (simulation only).

Run:
  uv run streamlit run saas/ui_streamlit.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib.pyplot as plt
import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dc_motor.registry import DEFAULT_PLANT_ID, get_plant_spec, list_plants  # noqa: E402
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

/* ----- Sidebar: dark panel, but keep INPUT text dark on white ----- */
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
/* Selected value inside dropdowns must stay dark (white field). */
[data-testid="stSidebar"] [data-baseweb="select"] * { color: #10232b !important; }
/* Slider labels/values are fine light. */
[data-testid="stSidebar"] [data-testid="stSlider"] * { color: #dbe6ea !important; }

.cdc-brand { font-size: 1.55rem; font-weight: 700; color: #102028; margin: 0; }
.cdc-sub { color: #5a6d76; margin: 0.2rem 0 0.9rem 0; }

.cdc-steps { display: flex; gap: 0.5rem; margin: 0 0 1.1rem 0; }
.cdc-step {
  flex: 1; padding: 0.6rem 0.8rem; border-radius: 10px;
  border: 1px solid #c9d4db; background: rgba(255,255,255,0.65); color: #5a6d76;
  font-size: 0.9rem;
}
.cdc-step strong { display: block; color: #102028; font-size: 0.95rem; margin-bottom: 0.1rem; }
.cdc-step.active { background: #12323a; color: #cfe7e1; border-color: #12323a; }
.cdc-step.active strong { color: #ffffff; }
.cdc-step.done { border-color: #9ed4c2; background: #e8f6f1; color: #2c6b5b; }
.cdc-step.done strong { color: #0f5c48; }

.cdc-card {
  background: rgba(255,255,255,0.9); border: 1px solid #c9d4db;
  border-radius: 12px; padding: 1.05rem 1.15rem 1.2rem 1.15rem;
  box-shadow: 0 10px 26px rgba(16, 32, 40, 0.06); margin-bottom: 0.85rem;
}
.cdc-card h3 { margin: 0 0 0.3rem 0; color: #102028; }
.cdc-card p.lead { margin: 0 0 0.9rem 0; color: #5a6d76; }
.cdc-pill {
  display: inline-block; padding: 0.25rem 0.7rem; border-radius: 999px;
  background: #edf2f4; color: #314650; border: 1px solid #c5d0d6;
  font-size: 0.8rem; margin-bottom: 0.8rem;
}
.stButton > button[kind="primary"] {
  background: #0f766e !important; border-color: #0f766e !important;
}
</style>
""",
    unsafe_allow_html=True,
)

SAMPLE_GOALS = [
    (
        "Gentle speed step",
        "Settle under 1.5 s, overshoot under 10%, keep |voltage| ≤ 12 V, prefer low control effort.",
    ),
    (
        "Faster with load",
        "Settle under 1.2 s, overshoot under 8%, reject a small load (~0.01 N·m) after 1.5 s, |voltage| ≤ 12 V.",
    ),
    (
        "Robustness focus",
        "Tight step response, overshoot under 5%, prioritize robustness to plant parameter mismatch.",
    ),
]

# Plain-language quick edits offered on the Results step.
QUICK_FIXES = [
    ("Retune gains", "Please retune the controller."),
    ("Relax settling", "Relax the settling-time requirement to 2.5 s."),
    ("Add load disturbance", "Add a load disturbance test."),
    ("Make more robust", "Make the design more robust to plant mismatch."),
]


def _ensure_job():
    if "job_id" not in st.session_state:
        job = service.create_job(
            plant_id=st.session_state.get("plant_id", DEFAULT_PLANT_ID),
            mode=st.session_state.get("mode", "heuristic"),
        )
        st.session_state.job_id = job.job_id
    return get_job_store().get(st.session_state.job_id)


def _active_step(job) -> int:
    """1 = Goals, 2 = Requirements, 3 = Results."""
    if job.status in {"completed", "exported"} or (job.status == "failed" and job.session_dict):
        return 3
    if job.status == "spec_ready":
        return 2
    return 1  # draft or needs_clarification


def _step_bar(active: int) -> None:
    labels = [
        (1, "1 · Your goals", "Describe the behavior you want"),
        (2, "2 · Requirements", "Check the tests to be run"),
        (3, "3 · Results", "See performance & export"),
    ]
    cells = []
    for num, title, blurb in labels:
        cls = "cdc-step"
        if num == active:
            cls += " active"
        elif num < active:
            cls += " done"
        cells.append(f'<div class="{cls}"><strong>{title}</strong>{blurb}</div>')
    st.markdown(f'<div class="cdc-steps">{"".join(cells)}</div>', unsafe_allow_html=True)


def _plot_scorecard(scorecard: dict) -> None:
    if not scorecard or "scenarios" not in scorecard:
        return
    fig, axes = plt.subplots(2, 1, figsize=(7.5, 5.0), sharex=True)
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


def _render_requirements(spec_dict: dict) -> None:
    st.markdown("**Must-meet performance limits**")
    lines = constraints_as_lines(spec_dict)
    if lines:
        for line in lines:
            st.markdown(f"- {line}")
    else:
        st.caption("No explicit limits — sensible defaults will apply.")

    st.markdown("**Simulation tests to run**")
    for line in scenarios_as_lines(spec_dict):
        st.markdown(f"- {line}")

    st.markdown("**Operating setup**")
    for k, v in limits_summary(spec_dict).items():
        st.markdown(f"- {k}: **{v}**")

    warns = spec_dict.get("warnings") or []
    if warns:
        st.warning("Heads-up from the requirement check:\n\n" + "\n".join(f"- {w}" for w in warns))


def _render_controller(best: dict) -> None:
    params = best.get("params") or {}
    st.write(controller_name(best.get("kind")))
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


def _run_feedback(job, text: str) -> None:
    try:
        with st.spinner("Updating the design…"):
            service.apply_feedback_and_maybe_rerun(job, text, use_llm=False, rerun=True)
        st.rerun()
    except Exception as exc:  # noqa: BLE001
        st.error(str(exc))


# ============================ Sidebar ============================
with st.sidebar:
    st.markdown("### Project setup")
    st.caption("Pick the system to control and how redesign decisions are made.")

    plant_ids = [p.plant_id for p in list_plants()]
    plant_id = st.selectbox(
        "Plant (system to control)",
        options=plant_ids,
        index=plant_ids.index(st.session_state.get("plant_id", DEFAULT_PLANT_ID)),
        format_func=lambda pid: get_plant_spec(pid).name,
    )
    st.caption(get_plant_spec(plant_id).description)

    mode_keys = list(DESIGN_STRATEGY.keys())
    mode = st.selectbox(
        "Design strategy",
        options=mode_keys,
        index=mode_keys.index(st.session_state.get("mode", "heuristic")),
        format_func=lambda m: DESIGN_STRATEGY[m]["label"],
    )
    st.caption(DESIGN_STRATEGY[mode]["help"])

    max_iter = st.slider(
        "Max redesign attempts",
        1,
        12,
        int(st.session_state.get("max_iter", 5)),
        help="How many tune / redesign tries are allowed in one design run.",
    )

    if st.button("Start over", use_container_width=True, type="primary"):
        job = service.create_job(plant_id=plant_id, mode=mode)
        job.max_iterations = max_iter
        st.session_state.job_id = job.job_id
        st.session_state.plant_id = plant_id
        st.session_state.mode = mode
        st.session_state.max_iter = max_iter
        st.session_state.pop("goal_draft", None)
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
            "Strategies",
            options=mode_keys,
            default=["script", "heuristic"],
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
st.session_state.plant_id = plant_id
st.session_state.mode = mode
st.session_state.max_iter = max_iter
job.plant_id = plant_id
job.mode = mode
job.max_iterations = max_iter
plant = get_plant_spec(job.plant_id)
active = _active_step(job)

# ============================ Header ============================
st.markdown('<p class="cdc-brand">Control Design Copilot</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="cdc-sub">Design a closed-loop controller for a simulated plant — no hardware. '
    "The simulator computes every metric; the assistant only plans and explains.</p>",
    unsafe_allow_html=True,
)
st.markdown(f'<span class="cdc-pill">{job_status_label(job.status)}</span>', unsafe_allow_html=True)
_step_bar(active)

# ======================= STEP 1: Goals =======================
if active == 1:
    st.markdown('<div class="cdc-card">', unsafe_allow_html=True)
    st.markdown("### Step 1 — Describe your control goals")
    st.markdown(
        '<p class="lead">Write what you want the closed loop to do, in plain English: '
        "settling time, overshoot, disturbance rejection, voltage limits, etc.</p>",
        unsafe_allow_html=True,
    )

    if job.status == "needs_clarification" and job.clarifying_questions:
        st.info("Please answer these so I can finish the requirements, then submit again:")
        for q in job.clarifying_questions:
            st.markdown(f"- {q}")

    st.markdown("**Examples** (click to fill the box)")
    eg_cols = st.columns(len(SAMPLE_GOALS))
    for i, (title, text) in enumerate(SAMPLE_GOALS):
        with eg_cols[i]:
            if st.button(title, key=f"eg_{i}", use_container_width=True, help=text):
                st.session_state["goal_draft"] = text
                st.rerun()

    goal = st.text_area(
        "Your performance goals",
        value=st.session_state.get("goal_draft", ""),
        height=110,
        placeholder="e.g. Settle under 1.5 s, overshoot under 10%, keep |voltage| ≤ 12 V…",
        label_visibility="collapsed",
    )

    label = "Submit answer" if job.status == "needs_clarification" else "Translate goals → requirements"
    if st.button(label, type="primary"):
        text = (goal or "").strip()
        if not text:
            st.warning("Please enter your goals first.")
        else:
            try:
                with st.spinner("Reading your goals…"):
                    if job.status == "needs_clarification":
                        service.answer_clarification(job, text)
                    else:
                        service.interpret_job_spec(job, text, critique=True)
                st.session_state.pop("goal_draft", None)
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))

    if job.chat:
        with st.expander("Conversation", expanded=False):
            for msg in job.chat:
                who = "You" if msg.get("role") == "user" else "Copilot"
                st.markdown(f"**{who}:** {msg.get('content', '')}")
    st.markdown("</div>", unsafe_allow_html=True)

# =================== STEP 2: Requirements ===================
elif active == 2:
    st.markdown('<div class="cdc-card">', unsafe_allow_html=True)
    st.markdown("### Step 2 — Review the requirements")
    st.markdown(
        '<p class="lead">These formal limits and tests were derived from your goals. '
        "Designing runs the controller against every test below.</p>",
        unsafe_allow_html=True,
    )
    st.caption(f"Plant: **{plant.name}** · Strategy: **{DESIGN_STRATEGY[job.mode]['label']}**")
    if job.spec_dict:
        _render_requirements(job.spec_dict)

    c1, c2 = st.columns([1, 1])
    with c1:
        if st.button("Design controller", type="primary", use_container_width=True):
            try:
                with st.spinner("Designing and running all simulation tests…"):
                    service.confirm_and_run(job, max_iterations=max_iter)
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(str(exc))
    with c2:
        if st.button("◀ Back to goals", use_container_width=True):
            job.status = "draft"
            job.confirmed = False
            job.touch()
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

# ====================== STEP 3: Results ======================
else:
    best = (job.session_dict or {}).get("best") or {}
    all_pass = bool(best.get("all_pass"))
    kind = best.get("kind")
    sess_status = (job.session_dict or {}).get("status")

    # --- Outcome ---
    st.markdown('<div class="cdc-card">', unsafe_allow_html=True)
    st.markdown("### Step 3 — Results")
    if all_pass:
        st.success(f"**Requirements met.** {controller_name(kind)} passed every required test.")
    elif job.session_dict:
        st.error(
            f"**Not fully met.** Best attempt: {controller_name(kind)}. {session_outcome(sess_status)}"
        )
    elif job.error:
        st.error(job.error)
    else:
        st.warning("No results yet.")

    if job.certification:
        if job.certification.get("allowed"):
            st.caption("✅ Certification gate: eligible to export a simulation package.")
        else:
            st.caption(f"⛔ Certification gate: blocked — {job.certification.get('reason', 'requirements not met.')}")

    if best:
        st.markdown("#### Controller")
        _render_controller(best)
    st.markdown("</div>", unsafe_allow_html=True)

    # --- Test results ---
    if job.scorecard:
        st.markdown('<div class="cdc-card">', unsafe_allow_html=True)
        st.markdown("#### How it performed")
        rows = scorecard_rows(job.scorecard)
        if rows:
            st.dataframe(rows, use_container_width=True, hide_index=True)
        _plot_scorecard(job.scorecard)
        with st.expander("Measured numbers & design steps", expanded=False):
            for item in job.scorecard.get("scenarios", []):
                metrics = item.get("metrics") or {}
                st.markdown(f"**{item.get('name', 'test')}**")
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
                st.markdown("**What the designer tried**")
                for a in trace:
                    act = str(a.get("action", "")).replace("_", " ")
                    mark = "✓" if a.get("all_pass") else "✗"
                    st.markdown(f"- Attempt {a.get('iteration')}: {act} ({mark})")
            if (job.session_dict or {}).get("rationale"):
                st.markdown("**Design rationale**")
                st.write(job.session_dict["rationale"])
        st.markdown("</div>", unsafe_allow_html=True)

    # --- Adjust the design ---
    st.markdown('<div class="cdc-card">', unsafe_allow_html=True)
    st.markdown("#### Change something")
    st.caption("Pick a quick fix, or type a request. Each change re-runs the tests.")
    qcols = st.columns(len(QUICK_FIXES))
    for i, (label, phrase) in enumerate(QUICK_FIXES):
        with qcols[i]:
            if st.button(label, key=f"qf_{i}", use_container_width=True):
                _run_feedback(job, phrase)

    fb = st.text_input(
        "Custom request",
        placeholder="e.g. reduce overshoot / add a load disturbance / relax settling to 2 s",
        label_visibility="collapsed",
    )
    b1, b2, b3 = st.columns([1, 1, 1])
    with b1:
        if st.button("Apply request", use_container_width=True) and fb.strip():
            _run_feedback(job, fb.strip())
    with b2:
        if st.button("Accept design", type="primary", use_container_width=True):
            _run_feedback(job, "Accept this design.")
    with b3:
        if st.button("◀ Edit goals", use_container_width=True):
            job.status = "draft"
            job.confirmed = False
            job.session_dict = None
            job.scorecard = None
            job.certification = None
            job.touch()
            st.rerun()
    st.markdown("</div>", unsafe_allow_html=True)

    # --- Export ---
    st.markdown('<div class="cdc-card">', unsafe_allow_html=True)
    st.markdown("#### Export")
    st.caption("Download a certification package (controller + scorecard + rationale).")
    if st.button("Download certification package", use_container_width=False):
        try:
            path = service.export_job(job)
            st.success(f"Saved to `{path}`")
        except Exception as exc:  # noqa: BLE001
            st.error(str(exc))
    if job.chat:
        with st.expander("Full conversation", expanded=False):
            for msg in job.chat:
                who = "You" if msg.get("role") == "user" else "Copilot"
                st.markdown(f"**{who}:** {msg.get('content', '')}")
    st.markdown("</div>", unsafe_allow_html=True)
