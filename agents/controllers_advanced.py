"""Real model-based / intelligent controllers (workstream C).

Every controller here honours the project-wide interface

    reset() -> None
    step(measurement, reference, dt) -> u

and exposes ``name`` + ``last_saturated`` so the deterministic evaluation harness
(``dc_motor.evaluate_controller``) can score them exactly like the PID baseline.

Families implemented (all *simulation only* — no hardware):

  * ``StateFeedbackServoController`` — integral-augmented optimal state feedback
    (LQI) driven by a state observer. Used for both LQR (Luenberger observer via
    ``control.place``) and LQG (Kalman observer via ``control.lqe``).
  * ``MPCController`` — a *proper* constrained model-predictive controller: a
    condensed QP (``cvxpy`` + OSQP) solved in receding horizon over the
    discretized motor model, with hard input-voltage bounds and a move-suppression
    term. This replaces the old toy line-search MPC.
  * ``MRACController`` — Lyapunov model-reference adaptive control (feedforward +
    feedback gains adapted online against a first-order reference model, with
    sigma-modification for boundedness). A real MRAC, well beyond "Ki-lite".
  * ``FuzzyPIDController`` — a Takagi–Sugeno fuzzy gain-scheduling PID: triangular
    membership on the (normalized) error magnitude schedules Kp/Ki/Kd online.

The heavy control libraries are imported lazily so the package still imports when
they are absent; the *designers* (see ``agents.specialists``) surface a clear error.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np


# --------------------------------------------------------------------------- #
# Small numerical helpers (observer / gain design)
# --------------------------------------------------------------------------- #
def _clip(u: float, lo: float, hi: float) -> float:
    return float(min(hi, max(lo, u)))


def luenberger_observer_gain(
    A: np.ndarray,
    C: np.ndarray,
    *,
    dt_nom: float = 1e-3,
    speed_factor: float = 4.0,
) -> np.ndarray:
    """Deterministic Luenberger observer gain via pole placement.

    Observer poles are placed a few times faster than the plant's fastest mode,
    but capped relative to the nominal step ``dt_nom`` so a forward-Euler observer
    stays numerically stable for arbitrary motors.
    """
    from control import place

    eig = np.linalg.eigvals(A)
    fastest = float(np.max(np.abs(eig.real))) or 1.0
    mag = speed_factor * fastest
    # Cap so dt_nom * pole stays well inside the Euler stability region.
    mag = min(mag, 0.1 / max(dt_nom, 1e-9))
    mag = max(mag, 2.0 * fastest)
    poles = [-mag, -mag * 1.25]  # distinct reals required by place()
    L = place(A.T, C.T, poles).T
    return np.asarray(L, dtype=float)


def kalman_observer_gain(
    A: np.ndarray,
    C: np.ndarray,
    *,
    process_var: float = 1.0,
    meas_var: float = 1e-3,
) -> np.ndarray:
    """Steady-state Kalman observer gain (continuous) via ``control.lqe``."""
    from control import lqe

    n = A.shape[0]
    G = np.eye(n)
    QN = process_var * np.eye(n)
    RN = np.array([[max(meas_var, 1e-9)]], dtype=float)
    L, _P, _E = lqe(A, G, C, QN, RN)
    return np.asarray(L, dtype=float)


# --------------------------------------------------------------------------- #
# LQR / LQG — integral-augmented optimal state feedback with an observer
# --------------------------------------------------------------------------- #
class StateFeedbackServoController:
    """LQI (LQR + integral action) with a state observer.

    Control law (measured output y = omega):

        x̂ ← observer(x̂, u, y)                       # reconstruct [i, omega]
        xi ← xi + (r - y) dt                          # tracking integral (anti-windup)
        u  = -Kx x̂ - ki xi                            # optimal feedback, clamped

    The gains ``Kx`` (state) and ``ki`` (integral) come from an LQR solved on the
    integral-augmented plant; ``L`` is the observer gain (Luenberger for LQR,
    Kalman for LQG).
    """

    def __init__(
        self,
        *,
        A: np.ndarray,
        B: np.ndarray,
        C: np.ndarray,
        Kx: np.ndarray,
        ki: float,
        L: np.ndarray,
        V_min: float = -12.0,
        V_max: float = 12.0,
        name: str = "LQR",
    ) -> None:
        self.A = np.asarray(A, dtype=float)
        self.B = np.asarray(B, dtype=float).reshape(-1, 1)
        self.C = np.asarray(C, dtype=float).reshape(1, -1)
        self.Kx = np.asarray(Kx, dtype=float).reshape(1, -1)
        self.ki = float(ki)
        self.L = np.asarray(L, dtype=float).reshape(-1, 1)
        self.V_min = float(V_min)
        self.V_max = float(V_max)
        self.name = name
        self.reset()

    def reset(self) -> None:
        n = self.A.shape[0]
        self._xhat = np.zeros((n, 1), dtype=float)
        self._xi = 0.0
        self._u_prev = 0.0
        self.last_saturated = False

    def step(self, measurement: float, reference: float, dt: float) -> float:
        y = float(measurement)
        r = float(reference)

        # Observer correction/prediction (forward Euler on the continuous model).
        xdot = self.A @ self._xhat + self.B * self._u_prev + self.L * (y - float((self.C @ self._xhat)[0, 0]))
        self._xhat = self._xhat + xdot * dt

        e = r - y
        u_unsat = float(-(self.Kx @ self._xhat)[0, 0] - self.ki * self._xi)
        u = _clip(u_unsat, self.V_min, self.V_max)
        saturated = u != u_unsat
        self.last_saturated = saturated

        # Conditional anti-windup: integrate unless saturating further.
        if (not saturated) or (u_unsat > self.V_max and e < 0) or (u_unsat < self.V_min and e > 0):
            self._xi += e * dt

        self._u_prev = u
        return u


# --------------------------------------------------------------------------- #
# MPC — constrained receding-horizon QP (cvxpy + OSQP)
# --------------------------------------------------------------------------- #
class MPCController:
    """Constrained model-predictive controller (condensed QP, receding horizon).

    A digital MPC that samples at control period ``Ts`` (>= the simulation dt).
    At each control instant it solves

        min_U  q Σ (y_k - r)^2 + r_u Σ u_k^2 + r_du Σ (u_k - u_{k-1})^2
        s.t.   x_{k+1} = Ad x_k + Bd u_k ,   V_min <= u_k <= V_max

    over an N-step horizon on the discretized motor model, applies the first move
    (receding horizon), and holds it until the next control instant. A state
    observer reconstructs [i, omega] from the measured speed between solves.

    Hard voltage constraints are enforced *inside* the optimizer — the defining
    advantage over PID/line-search. Uses ``cvxpy`` with OSQP and a parametric
    problem (warm-started) so per-step solves stay in the sub-millisecond range.
    """

    def __init__(
        self,
        *,
        Ad: np.ndarray,
        Bd: np.ndarray,
        Cd: np.ndarray,
        A_cont: np.ndarray,
        B_cont: np.ndarray,
        L: np.ndarray,
        Ts: float,
        horizon: int = 25,
        q_track: float = 1.0,
        r_u: float = 1e-3,
        r_du: float = 1e-2,
        L_d: float = 40.0,
        V_min: float = -12.0,
        V_max: float = 12.0,
        name: str = "MPC",
    ) -> None:
        self.Ad = np.asarray(Ad, dtype=float)
        self.Bd = np.asarray(Bd, dtype=float).reshape(-1, 1)
        self.Cd = np.asarray(Cd, dtype=float).reshape(1, -1)
        self.A_cont = np.asarray(A_cont, dtype=float)
        self.B_cont = np.asarray(B_cont, dtype=float).reshape(-1, 1)
        self.L = np.asarray(L, dtype=float).reshape(-1, 1)
        self.Ts = float(Ts)
        self.N = int(horizon)
        self.q_track = float(q_track)
        self.r_u = float(r_u)
        self.r_du = float(r_du)
        self.L_d = float(L_d)
        self.V_min = float(V_min)
        self.V_max = float(V_max)
        self.name = name
        self._build_problem()
        self.reset()

    # -- condensed prediction matrices + parametric cvxpy problem -------------
    def _build_problem(self) -> None:
        import cvxpy as cp

        nx = self.Ad.shape[0]
        N = self.N
        # Output prediction: Y = Sx x0 + Su U   (rows k = 1..N)
        Sx = np.zeros((N, nx))
        Su = np.zeros((N, N))
        Apow = [np.eye(nx)]
        for _ in range(N):
            Apow.append(self.Ad @ Apow[-1])
        for k in range(1, N + 1):
            Sx[k - 1, :] = (self.Cd @ Apow[k]).ravel()
            for j in range(k):
                Su[k - 1, j] = float((self.Cd @ Apow[k - 1 - j] @ self.Bd)[0, 0])
        self._Sx = Sx
        self._Su = Su

        # Steady-state input that holds output = reference (offset-free tracking):
        #   y_ss = Cd (I - Ad)^{-1} Bd u_ss = r  ->  u_ss = Kss * r
        dc = float((self.Cd @ np.linalg.solve(np.eye(nx) - self.Ad, self.Bd))[0, 0])
        self._Kss = 1.0 / dc if abs(dc) > 1e-12 else 0.0

        self._U = cp.Variable(N)
        self._x0 = cp.Parameter(nx)
        self._r = cp.Parameter()
        self._u_prev_p = cp.Parameter()

        u_ss = self._Kss * self._r
        Y = Sx @ self._x0 + Su @ self._U
        cost = self.q_track * cp.sum_squares(Y - self._r)
        # Penalize deviation from the correct steady input (removes offset).
        cost += self.r_u * cp.sum_squares(self._U - u_ss)
        # Move suppression, including the jump from the previously applied input.
        du = cp.hstack([self._U[0] - self._u_prev_p, cp.diff(self._U)])
        cost += self.r_du * cp.sum_squares(du)
        constraints = [self._U >= self.V_min, self._U <= self.V_max]
        self._prob = cp.Problem(cp.Minimize(cost), constraints)

    def reset(self) -> None:
        n = self.Ad.shape[0]
        self._xhat = np.zeros((n, 1), dtype=float)
        self._d_hat = 0.0  # estimated constant output disturbance (offset-free)
        self._u_hold = 0.0
        self._t_since_solve = math.inf  # force a solve on the first step
        self.last_saturated = False

    def _solve(self, reference: float) -> float:
        import cvxpy as cp

        self._x0.value = self._xhat.ravel()
        # Offset-free: track the reference less the estimated output disturbance.
        self._r.value = float(reference) - self._d_hat
        self._u_prev_p.value = float(self._u_hold)
        try:
            self._prob.solve(solver=cp.OSQP, warm_start=True, verbose=False)
            u_seq = self._U.value
            if u_seq is None or not np.all(np.isfinite(u_seq)):
                return self._u_hold
            return _clip(float(u_seq[0]), self.V_min, self.V_max)
        except Exception:  # noqa: BLE001 — never let a solver hiccup crash the sim
            return self._u_hold

    def step(self, measurement: float, reference: float, dt: float) -> float:
        y = float(measurement)

        # Observer with an integrating output-disturbance estimate (offset-free
        # tracking under constant loads / model mismatch).
        innovation = y - self._d_hat - float((self.Cd @ self._xhat)[0, 0])
        xdot = self.A_cont @ self._xhat + self.B_cont * self._u_hold + self.L * innovation
        self._xhat = self._xhat + xdot * dt
        self._d_hat += self.L_d * innovation * dt

        # Re-solve the QP at the control period; hold between instants (ZOH).
        self._t_since_solve += dt
        if self._t_since_solve >= self.Ts - 1e-12:
            self._u_hold = self._solve(reference)
            self._t_since_solve = 0.0

        self.last_saturated = abs(self._u_hold) >= abs(self.V_max) - 1e-9
        return self._u_hold


# --------------------------------------------------------------------------- #
# MRAC — Lyapunov model-reference adaptive control
# --------------------------------------------------------------------------- #
class MRACController:
    """Model-reference adaptive control for the speed loop.

    A first-order reference model ``y_m`` defines the desired closed-loop response

        ẏ_m = a_m (r - y_m)              (time constant 1/a_m)

    and the control law with online-adapted gains is

        u  = k_r r + k_y y
        k̇_r = -γ_r e r - σ k_r          (Lyapunov update + σ-modification)
        k̇_y = -γ_y e y - σ k_y
        e   = y - y_m

    This adapts to unknown DC gain / time constant of the motor (and drift under
    load), giving zero steady-state tracking error without a fixed model — a real
    MRAC, not an integral-gain tweak.
    """

    def __init__(
        self,
        *,
        a_m: float,
        kr0: float = 0.0,
        ky0: float = 0.0,
        gamma_r: float = 2.0,
        gamma_y: float = 2.0,
        sigma: float = 1e-3,
        V_min: float = -12.0,
        V_max: float = 12.0,
        name: str = "MRAC",
    ) -> None:
        self.a_m = float(a_m)
        self.kr0 = float(kr0)
        self.ky0 = float(ky0)
        self.gamma_r = float(gamma_r)
        self.gamma_y = float(gamma_y)
        self.sigma = float(sigma)
        self.V_min = float(V_min)
        self.V_max = float(V_max)
        self.name = name
        self.reset()

    def reset(self) -> None:
        self._ym = 0.0
        self._kr = self.kr0
        self._ky = self.ky0
        self.last_saturated = False

    def step(self, measurement: float, reference: float, dt: float) -> float:
        y = float(measurement)
        r = float(reference)

        # Reference model integration.
        self._ym += self.a_m * (r - self._ym) * dt
        e = y - self._ym

        u_unsat = self._kr * r + self._ky * y
        u = _clip(u_unsat, self.V_min, self.V_max)
        saturated = u != u_unsat
        self.last_saturated = saturated

        # Normalized Lyapunov update (bounded regressor); frozen while saturated.
        if not saturated:
            denom = 1.0 + r * r + y * y
            self._kr += (-self.gamma_r * e * r / denom - self.sigma * self._kr) * dt
            self._ky += (-self.gamma_y * e * y / denom - self.sigma * self._ky) * dt

        return u


# --------------------------------------------------------------------------- #
# Fuzzy PID — Takagi–Sugeno gain scheduling
# --------------------------------------------------------------------------- #
def _tri(x: float, a: float, b: float, c: float) -> float:
    """Triangular membership value at ``x`` for a triangle (a, b, c)."""
    if x <= a or x >= c:
        return 0.0
    if x == b:
        return 1.0
    if x < b:
        return (x - a) / (b - a)
    return (c - x) / (c - b)


class FuzzyPIDController:
    """Fuzzy gain-scheduling PID (Takagi–Sugeno).

    The absolute tracking error is normalized by the reference and mapped through
    three triangular fuzzy sets (SMALL / MEDIUM / LARGE). Their memberships blend
    per-set gain multipliers so the controller is aggressive far from setpoint
    (high Kp, low Ki to avoid windup) and well-damped near it (higher Kd/Ki to
    kill steady-state error). Base gains come from the classical warm start.
    """

    def __init__(
        self,
        *,
        Kp: float,
        Ki: float,
        Kd: float,
        e_scale: float = 1.0,
        V_min: float = -12.0,
        V_max: float = 12.0,
        name: str = "FuzzyPID",
    ) -> None:
        self.Kp = float(Kp)
        self.Ki = float(Ki)
        self.Kd = float(Kd)
        self.e_scale = max(float(e_scale), 1e-6)
        self.V_min = float(V_min)
        self.V_max = float(V_max)
        self.name = name
        # Per-fuzzy-set multipliers for (Kp, Ki, Kd): SMALL, MEDIUM, LARGE error.
        self._rules = {
            "small": (0.8, 1.4, 1.3),
            "medium": (1.1, 1.0, 1.0),
            "large": (1.5, 0.3, 0.7),
        }
        self.reset()

    def reset(self) -> None:
        self._integ = 0.0
        self._e_prev = 0.0
        self._initialized = False
        self.last_saturated = False

    def _schedule(self, e_norm: float) -> tuple[float, float, float]:
        a = abs(e_norm)
        mu_small = _tri(a, -0.5, 0.0, 0.5)
        mu_med = _tri(a, 0.2, 0.6, 1.0)
        mu_large = 1.0 if a >= 1.0 else _tri(a, 0.6, 1.0, 1.4)
        total = mu_small + mu_med + mu_large
        if total <= 1e-9:
            mu_small, total = 1.0, 1.0
        fp = fi = fd = 0.0
        for mu, key in ((mu_small, "small"), (mu_med, "medium"), (mu_large, "large")):
            gp, gi, gd = self._rules[key]
            fp += mu * gp
            fi += mu * gi
            fd += mu * gd
        return fp / total, fi / total, fd / total

    def step(self, measurement: float, reference: float, dt: float) -> float:
        e = float(reference) - float(measurement)
        e_norm = e / self.e_scale
        fp, fi, fd = self._schedule(e_norm)
        kp, ki, kd = self.Kp * fp, self.Ki * fi, self.Kd * fd

        de = (e - self._e_prev) / dt if self._initialized else 0.0
        u_unsat = kp * e + ki * self._integ + kd * de
        u = _clip(u_unsat, self.V_min, self.V_max)
        saturated = u != u_unsat
        self.last_saturated = saturated

        if (not saturated) or (u_unsat > self.V_max and e < 0) or (u_unsat < self.V_min and e > 0):
            self._integ += e * dt

        self._e_prev = e
        self._initialized = True
        return u


__all__ = [
    "StateFeedbackServoController",
    "MPCController",
    "MRACController",
    "FuzzyPIDController",
    "luenberger_observer_gain",
    "kalman_observer_gain",
]
