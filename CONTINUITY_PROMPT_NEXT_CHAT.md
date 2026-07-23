# Continuity prompt — paste into a new chat

> **Status (2026-07-23) — Phase E2.3 (Async design runs via RQ) DONE.** CPU-heavy design
> runs are now enqueued to the RQ worker so FastAPI stays responsive. New: `saas/queue.py`
> — `get_redis_connection`/`get_queue` (queue = `settings.design_queue`, default `copilot`),
> module-level worker task `run_design_job(job_id, …)` (rehydrates the job from the active
> store and calls `service.confirm_and_run` — never receives a live controller), plus
> `enqueue_design_run` + `fetch_queue_job`. `saas/service.py`: `enqueue_design_run(job)`
> marks the job `queued`, persists BEFORE enqueue (so the worker sees committed state / poll
> reflects it), records `queue_job_id`; `run_status(job)` poll payload; shared
> `_ensure_spec_for_run`. `saas/api.py`: `POST /jobs/{id}/run` enqueues when
> `async_runs_enabled` else runs inline; new `GET /jobs/{id}/status` poll path. `DesignJob`
> gained `queue_job_id` (+ `queued` status; in `data` JSON so **no migration**);
> `agents/workflow.py` treats `queued` as the `designing` phase. `saas/config.py`:
> `async_runs_enabled` (`COPILOT_ASYNC_RUNS`, default False — needs persistence; Compose api
> sets it true). Result crosses the worker→API boundary via the E2.2 serialize/rehydrate +
> `rev` contract. `tests/test_async_runs.py` (+9): enqueue marks queued, inline queue runs +
> persists, worker task in a fresh store + API rehydrate, RQ `SimpleWorker` burst drain, and
> both `/run` (async + sync) + `/status` routes — all over `fakeredis` + SQLite, OpenAI-free.
> **144 tests pass** (was 135). NEXT: **E2.4** — SSE endpoint + Redis pub/sub events
> (message.delta, tool.started/finished, workspace.updated, run.status, refusal, error;
> sse-starlette fanned out over Redis pub/sub; fakeredis in tests), then E2.5 auth.

```
Continue the project Agentic Orchestration of DC Motor Control (simulation/SaaS only — no hardware).

## Continuity
Read first:
- PROJECT_SEQUENCE.txt
- README.md
- CONTINUITY_PROMPT_NEXT_CHAT.md (this brief)
- Packages: dc_motor/, agents/, experiments/, saas/, examples/, tests/

Repo: https://github.com/abbasi07/agentic-dc-motor-control
Local: E:\Agentic AI Course\Projects\1_Agentic_Orchestration_of_DC_Motor_Control
Stack: uv + .venv, Python >=3.12, OpenAI via .env (OPENAI_API_KEY, OPENAI_MODEL=gpt-5.4-nano). Do not commit .env.
Control libs: python-control (LQR/LQG/Kalman), cvxpy + osqp (constrained MPC QP).

## Product vision (the goal we are building toward)
A chat-first "Control Design Copilot": the engineer (1) describes ANY DC motor and
performance specs in chat, (2) the app checks the model+specs are physically feasible
and pushes back until they are correct, (3) the engineer picks a controller type, and
(4) the app designs it, computes metrics/plots, and answers follow-up questions
("what was the settling time?") — all in one always-visible conversation. Target: a
solid, robust, commercial SaaS and a prime example of an agentic control-engineering
workflow. UI is deferred (React later); we are improving FUNCTIONALITY + AGENTIC WORKFLOW.

## Roadmap workstreams (agreed with user)
A. Generalize plant to arbitrary DC motor from chat            — DONE
B. Physics-based feasibility / validation of specs            — DONE
C. Controller registry + real controllers                     — DONE
     LQR/LQG (python-control), proper constrained MPC (cvxpy/OSQP, replaced the
     toy line-search), MRAC (Lyapunov), Fuzzy PID (Takagi–Sugeno). Kept PID +
     Robust PID. RL deferred (roadmap only). Pluggable registry + grounded critic.
D. Chat-first tool-calling Design Agent + query_results        — DONE
     OpenAI function-calling loop owning the session; tools = define_plant,
     set_spec, check_feasibility, design_controller(type), simulate, query_results,
     modify, export/certify. LLM plans/talks; tools compute every number.
E. SaaS hardening: persistence (SQLite/Postgres), auth/multi-tenant, async design
     runs (grid + differential_evolution are blocking/CPU-heavy), token budgets,
     rate limiting, structured logging/trace of every tool call.  — TODO  <-- START HERE (next)
F. Agent eval harness (extend experiments/ablation.py): reaches certified design?
     how many tool calls / tokens? query grounding tests.                      — TODO

Order: D -> C -> E, with F throughout. Backend-first. D + C DONE; Streamlit now
exposes the chat-first agent + custom motor + family pick (still a thin harness —
do NOT polish into a final UI; React later). E is next.

## Done this chat (A + B — do not redo unless broken)
- dc_motor/motor_model.py  — MotorModel + build_motor_model() validation (positive/finite/
    range warnings) + motor_characteristics() from transfer function
    omega/V = K / (J L s^2 + (J R + b L) s + (b R + K^2)):
    dc_gain, omega_max ceiling, tau_mech, tau_elec, wn, zeta, damping, poles.
- dc_motor/feasibility.py  — analyze_feasibility()/check_feasibility(params, spec) ->
    FeasibilityReport with severity-tagged issues (error/warning/info):
    REFERENCE_UNREACHABLE, REFERENCE_NEAR_CEILING, SETTLING/RISE_INFEASIBLE|_TIGHT,
    OVERSHOOT_SETTLING_CONFLICT, SETTLING_BEFORE_LOAD_ONSET. Deterministic; LLM only phrases.
    min_time_to_reference() = omega_ref*J*R/(K*V_max) is the physical lower bound used.
- agents/plant_agent.py    — interpret_plant(text) OpenAI-only NL->validated MotorModel
    (SI unit conversion; re-validated). Plus motor_model_from_dict() (no LLM).
- saas/jobs.py             — DesignJob gains motor_dict, feasibility, _motor fields.
- saas/service.py          — set_motor_from_text(), set_motor_from_params(),
    effective_motor_params() (custom motor overrides registry), _feasibility_for_job();
    interpret_job_spec() now runs feasibility and BLOCKS (needs_clarification) with
    plain-language pushback when infeasible; confirm_and_run() routes custom motor via
    base_params + plant_id=None.
- saas/api.py              — POST /jobs/{id}/motor (NL) and /jobs/{id}/motor/params (numbers).
- dc_motor/__init__.py, agents/__init__.py — exports updated.
- tests/test_motor_model.py, tests/test_feasibility.py — 13 new tests (no OpenAI).
- Full suite: 33 passed (was 20). Lints clean. Verified end-to-end: 24V custom motor,
    target 5 rad/s -> flagged infeasible (ceiling 2.295); target 1.5 rad/s -> PID passes.

## Done this chat (workstream D — do not redo unless broken)
- agents/design_agent.py — DesignAgentSession: OpenAI function-calling loop that OWNS a
    DesignJob and exposes the engine as 8 typed tools: define_plant (NL or numeric motor),
    set_spec (NL->DesignSpec + auto feasibility), check_feasibility, design_controller(type in
    {auto,pid,robust,lqr,lqg,mpc,mrac,fuzzy,adaptive}), simulate, query_results, modify, export.
    chat() runs the tool loop; tools are plain Python (deterministic, testable without OpenAI).
- query_results is DETERMINISTIC and grounded: it only returns numbers already present in the
    stored scorecard (metric/scenario/pass-fail lookups via alias maps + digit-free scenario
    phrases so no stray digits leak). scorecard_numbers() is the ground-truth set.
- saas/jobs.py — DesignJob gained _agent (lazy session). saas/service.py — get_agent_session()
    + agent_chat(). saas/api.py — POST /jobs/{id}/agent (OpenAI-only; 503 if key missing).
- agents/__init__.py exports DesignAgentSession, TOOL_SCHEMAS, CONTROLLER_TYPES, scorecard_numbers.
- tests/test_design_agent.py — 14 tests (no OpenAI), incl. grounding: query_results emits no
    number absent from the scorecard (parametrized). test_api.py — agent route smoke (mocked chat).
- Full suite: 47 passed (was 33). Lints clean.

## Done this chat (workstream C — do not redo unless broken)
- dc_motor/state_space.py — motor_state_space(params) -> continuous (A,B,C,D,E) realization
    (states [i, omega], output omega) + ZOH discretize(dt); shared by model-based controllers.
- agents/controllers_advanced.py — real controllers, all reset()/step():
    * StateFeedbackServoController — integral-augmented LQI + observer (LQR: Luenberger via
      control.place; LQG: Kalman via control.lqe).
    * MPCController — PROPER constrained receding-horizon QP (cvxpy/OSQP), hard |u|<=V_max in
      the optimizer, offset-free via output-disturbance estimator, digital Ts + horizon from
      motor characteristics. REPLACED the toy line-search MPC.
    * MRACController — Lyapunov model-reference adaptive control (normalized update + sigma-mod).
    * FuzzyPIDController — Takagi–Sugeno fuzzy gain scheduling on error magnitude.
- agents/specialists.py — design_lqr/lqg/mpc/mrac/fuzzy (+ kept design_robust_pid, plant ID);
    design_adaptive now returns MRAC. Shared _score() helper. Toy MPCController removed.
- agents/controller_registry.py — ControllerFamily (kind, type_name, action, label, description,
    designer, addresses_tags, aliases). CONTROLLER_FAMILIES, CONTROLLER_TYPE_NAMES, design_by_type,
    families_for_tags, registry_metadata. Single source of truth for families.
- agents/critic.py — diagnose(candidate, spec, tried) -> Diagnosis (ordered recommended_actions +
    families from FailureDigest tags). GROUNDED: reads only the digest, invents no numbers.
- agents/orchestrator.py — AVAILABLE_ACTIONS += call_lqr/call_lqg/call_mrac/call_fuzzy (call_mpc
    now real; call_rl kept as MRAC alias). Heuristic policy uses diagnose(); LLM prompt+payload get
    the family menu + grounded diagnosis. dc_motor/failure.py TAG_TO_ACTION_HINTS updated per family.
- agents/design_agent.py — CONTROLLER_TYPES sourced from registry; design_controller dispatches via
    design_by_type. saas/present.py + saas/feedback.py — labels/actions for lqr/lqg/mpc/mrac/fuzzy.
- Tests: tests/test_controllers_advanced.py, test_controller_registry.py, test_orchestrator_actions.py.
    Full suite: 93 passed (was 48). Lints clean. e2e heuristic loop verified to escalate
    PID -> scipy -> call_lqr -> call_robust -> call_lqg -> call_mrac -> expand/relax under a hard spec.

## Done after C (Streamlit chat-first UI — do not redo unless broken)
- saas/ui_streamlit.py rewritten as chat-first console (still a temporary harness; React later):
  always-visible conversation panel wired to service.agent_chat / DesignAgentSession;
  structured Motor tab (J,b,K,R,L,V_max — presets only pre-fill); Requirements tab (no-LLM
  structured DesignSpec + feasibility); Design tab with Auto or explicit family pick
  (PID/Robust/LQR/LQG/MPC/MRAC/Fuzzy from controller_registry); Results & export tab.
  Degrades gracefully without OPENAI_API_KEY (structured panel still drives the engine).

## Locked decisions (do not reverse)
- Simulation/software only; export = certification package, not hardware.
- Controller interface: reset(); step(measurement, reference, dt) -> u.
- New controller families are added via agents/controller_registry.py ONLY (one
  ControllerFamily entry auto-wires design_controller + orchestrator menus + labels).
- Tools compute metrics/pass-fail; LLM NEVER invents numbers from plots or prose.
- OpenAI-only for NL->spec (interpret_spec) and NL->motor (interpret_plant); no regex
  agent fallback. Feasibility/validation is deterministic physics (not an LLM path).
- Certification gate is code-enforced (all hard constraints pass); LLM may explain, never override.
- Registry plant IDs remain: dc_motor_ctms, first_order_lag, position_servo; custom motor
  uses plant_id="custom_dc_motor" and passes MotorParams directly.
- Streamlit is a temporary MVP harness; React/Next comes AFTER functionality is solid.
- Do NOT collapse product back to "LLM picks min score among fixed PIDs".
- Do NOT re-add Lab_*.ipynb without explicit request.

## How to run / test
UI:    uv run streamlit run saas/ui_streamlit.py   -> http://localhost:8501
API:   uv run uvicorn saas.api:app --port 8000     (/docs = Swagger only, not the GUI)
Tests: uv sync --group dev && uv run pytest -q

## THIS CHAT — start workstream E (unless user redirects)
SaaS hardening (still simulation-only). Likely items: persistence (SQLite/Postgres) for the
in-memory JobStore; auth / multi-tenant scoping of jobs; async design runs (grid +
differential_evolution + per-step MPC QPs are blocking/CPU-heavy — offload to a worker/thread
so the API stays responsive); token + iteration budgets; rate limiting; structured
logging/trace of every tool call (the design agent already keeps tool_log). Keep workstream F
(eval harness) going throughout: extend experiments/ablation.py to score the new families and
add query-grounding tests. Ask the user only if a decision is blocking.

Note on C (done): controller families live in agents/controller_registry.py. To add another
family, add ONE ControllerFamily entry (kind, type_name, action, designer returning a scored
DesignCandidate, addresses_tags) — design_controller, orchestrator menus, and SaaS labels wire
up automatically. Advanced controllers are in agents/controllers_advanced.py; designers in
agents/specialists.py; state-space model in dc_motor/state_space.py; grounded critic in
agents/critic.py.

### Do NOT
- Delete .env (local) or commit secrets.
- Remove the OpenAI-only spec/motor interpret paths.
- Turn feasibility into an LLM guess (it must stay deterministic physics).
- Collapse the controller registry back into hard-coded if/else dispatch.
- Large drive-by refactors unrelated to the chosen workstream.
- Polish Streamlit into a "final" UI (React will replace it) — keep it a thin harness.
```
