"""Lab 01 — CTMS plant, open-loop step, closed-loop PID + metrics.

Run from project root:
  uv run python examples/lab_01_plant_pid.py
  uv run python examples/lab_01_plant_pid.py --plot
"""

from __future__ import annotations

import argparse

import numpy as np
from scipy import signal

from _util import add_plot_arg, ensure_project_on_path, maybe_show

ensure_project_on_path()

from dc_motor import (  # noqa: E402
    CTMS_PARAMS,
    DCMotorPlant,
    PIDController,
    step_performance_metrics,
)


def open_loop_step(v_step: float = 1.0, t_final: float = 3.0):
    p = CTMS_PARAMS
    num = [p.K]
    den = [p.J * p.L, p.J * p.R + p.b * p.L, p.b * p.R + p.K**2]
    sys = signal.TransferFunction(num, den)
    t = np.linspace(0.0, t_final, 1001)
    t_out, omega = signal.step(sys, T=t)
    return t_out, v_step * omega


def closed_loop_pid(
    *,
    kp: float = 100.0,
    ki: float = 200.0,
    kd: float = 10.0,
    omega_ref: float = 1.0,
    dt: float = 0.001,
    t_final: float = 3.0,
) -> dict:
    plant = DCMotorPlant(CTMS_PARAMS)
    ctrl = PIDController(Kp=kp, Ki=ki, Kd=kd, name="PID_CTMS_baseline")
    plant.reset()
    ctrl.reset()

    n = int(np.round(t_final / dt)) + 1
    t = np.linspace(0.0, t_final, n)
    omega = np.zeros(n)
    u_hist = np.zeros(n)
    e_hist = np.zeros(n)
    sat = np.zeros(n, dtype=bool)

    for k in range(n):
        meas = plant.omega
        u = ctrl.step(meas, omega_ref, dt)
        plant.step(u, dt)
        omega[k] = plant.omega
        u_hist[k] = u
        e_hist[k] = omega_ref - meas
        sat[k] = ctrl.last_saturated

    return {
        "t": t,
        "omega": omega,
        "u": u_hist,
        "e": e_hist,
        "saturated": sat,
        "omega_ref": omega_ref,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_plot_arg(parser)
    args = parser.parse_args(argv)
    maybe_show(args.plot)

    p = CTMS_PARAMS
    print("DC motor parameters (CTMS):")
    print(f"  J={p.J}  b={p.b}  K={p.K}  R={p.R}  L={p.L}")

    t_ol, omega_ol = open_loop_step()
    print(f"Open-loop steady-state speed @ 1 V: {omega_ol[-1]:.4f} rad/s")

    sim = closed_loop_pid()
    metrics = step_performance_metrics(
        sim["t"], sim["omega"], sim["u"], sim["e"], sim["saturated"], sim["omega_ref"]
    )
    print("Closed-loop PID metrics (Kp=100, Ki=200, Kd=10):")
    for key, val in metrics.items():
        if isinstance(val, float) and (np.isnan(val) or np.isinf(val)):
            print(f"  {key}: n/a")
        else:
            print(f"  {key}: {val:.6g}")

    if args.plot:
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot(t_ol, omega_ol, lw=2)
        ax.set_title("Open-loop speed (1 V step)")
        ax.set_xlabel("Time [s]")
        ax.set_ylabel("omega [rad/s]")
        ax.grid(True, alpha=0.3)

        fig2, axes = plt.subplots(3, 1, figsize=(8, 8), sharex=True)
        axes[0].plot(sim["t"], sim["omega"], lw=2)
        axes[0].axhline(sim["omega_ref"], color="C1", ls="--")
        axes[0].set_ylabel("omega")
        axes[1].plot(sim["t"], sim["u"], color="C2", lw=2)
        axes[1].set_ylabel("u [V]")
        axes[2].plot(sim["t"], sim["e"], color="C3", lw=2)
        axes[2].set_ylabel("e")
        axes[2].set_xlabel("t [s]")
        for a in axes:
            a.grid(True, alpha=0.3)
        fig2.suptitle("Closed-loop PID")
        plt.show()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
