// Types mirroring the backend contracts. The frontend renders FIXED component types
// from this structured state; the LLM never authors UI or event types.
//
// Sources of truth (do not drift):
//   - Workspace snapshot:  agents/workflow.py :: build_workspace
//   - Event enum:          saas/events.py     :: EVENT_TYPES
//   - Job public dict:     saas/jobs.py       :: DesignJob.to_public_dict

// --------------------------------------------------------------------------- //
// Workflow phases (agents/workflow.py :: PHASE_ORDER)
// --------------------------------------------------------------------------- //
export const PHASE_ORDER = [
  "greeting",
  "motor_negotiation",
  "motor_agreed",
  "spec_negotiation",
  "controller_selection",
  "designing",
  "results_review",
  "exported",
] as const;

export type Phase = (typeof PHASE_ORDER)[number];

// --------------------------------------------------------------------------- //
// Fixed event enum (saas/events.py). The UI switches on exactly these.
// --------------------------------------------------------------------------- //
export type EventType =
  | "message.delta"
  | "tool.started"
  | "tool.finished"
  | "workspace.updated"
  | "run.status"
  | "refusal"
  | "error";

export interface CopilotEvent<T = Record<string, unknown>> {
  type: EventType;
  job_id: string;
  ts: number;
  data: T;
}

// --------------------------------------------------------------------------- //
// Workspace artifacts (agents/workflow.py). Only present artifacts appear.
// --------------------------------------------------------------------------- //
export interface MotorCharacteristics {
  dc_gain?: number;
  omega_max_rad_s?: number;
  tau_mech_s?: number;
  tau_elec_s?: number;
  wn_rad_s?: number;
  zeta?: number;
  damping?: string;
  [k: string]: unknown;
}

export interface MotorArtifact {
  name?: string;
  source?: string;
  params?: Record<string, number>;
  param_units?: Record<string, string>;
  V_max?: number;
  V_min?: number;
  characteristics?: MotorCharacteristics;
  warnings?: string[];
  confirmed?: boolean;
}

export type ConstraintOp = "<=" | ">=" | "==" | "<" | ">";

export interface HardConstraint {
  op: ConstraintOp;
  limit: number;
}

export interface SpecArtifact {
  raw_spec?: string;
  hard_constraints?: Record<string, HardConstraint>;
  soft_preferences?: Record<string, number>;
  required_scenarios?: string[];
  omega_ref?: number;
  V_max?: number;
  V_min?: number;
  t_final?: number;
  warnings?: string[];
  confirmed?: boolean;
}

export interface FeasibilityIssue {
  code?: string;
  severity?: "error" | "warning" | "info";
  message?: string;
  suggestion?: string;
}

export interface FeasibilityArtifact {
  feasible?: boolean;
  issues?: FeasibilityIssue[];
  characteristics?: MotorCharacteristics;
  [k: string]: unknown;
}

export interface ConstraintCheck {
  value: number;
  op: ConstraintOp;
  limit: number;
  pass: boolean;
}

export interface ScenarioResult {
  name?: string;
  metrics?: Record<string, number | null>;
  constraints?: { all_pass?: boolean; checks?: Record<string, ConstraintCheck> };
  scalar_score?: number;
}

export interface ResultsSummary {
  all_constraints_pass?: boolean;
  mean_scalar_score?: number;
  n_scenarios?: number;
  n_scenarios_pass?: number;
  pass_rate?: number;
  worst_case_ITAE?: number;
  min_pass?: boolean;
}

export interface ActionRecord {
  iteration?: number;
  action?: string;
  reason?: string;
  all_pass?: boolean;
  objective?: number;
  kind?: string;
  gains?: Record<string, number>;
  digest_summary?: string;
  policy?: string;
}

export interface ResultsArtifact {
  controller?: string;
  summary?: ResultsSummary;
  scenarios?: ScenarioResult[];
  constraints?: Record<string, HardConstraint>;
  session_status?: string;
  action_trace?: ActionRecord[];
  rationale?: string;
}

export interface PlotSeries {
  name?: string;
  t?: number[];
  omega?: number[];
  u?: number[];
  reference?: number[];
}

export interface PlotsArtifact {
  series?: PlotSeries[];
}

export interface CertificationArtifact {
  allowed?: boolean;
  reason?: string;
  controller_name?: string;
  kind?: string;
  params?: Record<string, number>;
  timestamp_utc?: string;
  [k: string]: unknown;
}

export interface ExportArtifact {
  path?: string;
  status?: string;
}

export interface WorkspaceArtifacts {
  motor?: MotorArtifact;
  spec?: SpecArtifact;
  feasibility?: FeasibilityArtifact;
  results?: ResultsArtifact;
  plots?: PlotsArtifact;
  certification?: CertificationArtifact;
  export?: ExportArtifact;
}

export type ArtifactKey = keyof WorkspaceArtifacts;

export interface Budgets {
  tokens_used?: number;
  max_iterations?: number;
  max_tokens_per_session?: number;
  max_design_iterations?: number;
  rate_limit_per_minute?: number;
  tokens_remaining?: number;
}

export interface Workspace {
  job_id?: string;
  phase: Phase;
  phase_label: string;
  status?: string;
  artifacts: WorkspaceArtifacts;
  open_tabs: ArtifactKey[];
  budgets: Budgets;
  error?: string | null;
}

// --------------------------------------------------------------------------- //
// Job public dict (saas/jobs.py :: DesignJob.to_public_dict)
// --------------------------------------------------------------------------- //
export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
}

export interface JobPublic {
  job_id: string;
  plant_id: string;
  status: string;
  nl_spec: string;
  mode: string;
  max_iterations: number;
  chat: ChatMessage[];
  clarifying_questions: string[];
  spec: SpecArtifact | null;
  motor: MotorArtifact | null;
  feasibility: FeasibilityArtifact | null;
  confirmed: boolean;
  motor_confirmed: boolean;
  spec_confirmed: boolean;
  session: Record<string, unknown> | null;
  scorecard_summary: ResultsSummary | null;
  certification: CertificationArtifact | null;
  export_path: string | null;
  error: string | null;
  queue_job_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface PlantPublic {
  plant_id: string;
  name?: string;
  description?: string;
  [k: string]: unknown;
}

// --------------------------------------------------------------------------- //
// Activity feed item (derived client-side from events — reflect-only)
// --------------------------------------------------------------------------- //
export interface ActivityItem {
  id: string;
  ts: number;
  kind: "tool" | "run" | "refusal" | "error";
  title: string;
  detail?: string;
  tone: "info" | "ok" | "warn" | "danger" | "running";
}
