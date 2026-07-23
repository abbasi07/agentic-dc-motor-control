"use client";

import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { downsample, fmtNum } from "@/lib/format";
import type { PlotSeries } from "@/lib/types";

interface Row {
  t: number;
  omega?: number;
  reference?: number;
  u?: number;
}

function buildRows(series: PlotSeries): Row[] {
  const t = series.t || [];
  const omega = series.omega || [];
  const u = series.u || [];
  const reference = series.reference || [];
  const rows: Row[] = t.map((tv, i) => ({
    t: tv,
    omega: omega[i],
    reference: reference[i],
    u: u[i],
  }));
  return downsample(rows, 400);
}

const AXIS = { stroke: "#5b6b82", fontSize: 11 };
const GRID = "#243044";

function ChartTooltip({ active, payload, label, unit }: any) {
  if (!active || !payload?.length) return null;
  return (
    <div className="rounded-md border border-ink-600 bg-ink-900 px-2.5 py-1.5 text-xs shadow-lg">
      <div className="mb-1 font-mono text-slate-400">t = {fmtNum(label, 3)} s</div>
      {payload.map((p: any) => (
        <div key={p.dataKey} className="flex items-center gap-2">
          <span className="h-2 w-2 rounded-full" style={{ background: p.color }} />
          <span className="text-slate-300">{p.name}:</span>
          <span className="font-mono text-slate-100">
            {fmtNum(p.value, 3)} {unit}
          </span>
        </div>
      ))}
    </div>
  );
}

export function TrajectoryChart({ series }: { series: PlotSeries }) {
  const rows = buildRows(series);

  return (
    <div className="space-y-4">
      <div>
        <h4 className="mb-1 text-xs font-medium text-slate-400">
          Speed ω (rad/s) vs time
        </h4>
        <div className="h-52 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={rows} margin={{ top: 8, right: 12, bottom: 4, left: -8 }}>
              <CartesianGrid stroke={GRID} strokeDasharray="3 3" />
              <XAxis
                dataKey="t"
                type="number"
                domain={["dataMin", "dataMax"]}
                tick={AXIS}
                tickFormatter={(v) => fmtNum(v, 2)}
                stroke={AXIS.stroke}
              />
              <YAxis tick={AXIS} stroke={AXIS.stroke} width={44} />
              <Tooltip content={<ChartTooltip unit="rad/s" />} />
              <Line
                type="monotone"
                dataKey="reference"
                name="reference"
                stroke="#d29922"
                strokeDasharray="4 3"
                dot={false}
                strokeWidth={1.5}
                isAnimationActive={false}
              />
              <Line
                type="monotone"
                dataKey="omega"
                name="ω"
                stroke="#4f9cf9"
                dot={false}
                strokeWidth={2}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>

      <div>
        <h4 className="mb-1 text-xs font-medium text-slate-400">
          Control effort u (V) vs time
        </h4>
        <div className="h-40 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={rows} margin={{ top: 8, right: 12, bottom: 4, left: -8 }}>
              <CartesianGrid stroke={GRID} strokeDasharray="3 3" />
              <XAxis
                dataKey="t"
                type="number"
                domain={["dataMin", "dataMax"]}
                tick={AXIS}
                tickFormatter={(v) => fmtNum(v, 2)}
                stroke={AXIS.stroke}
              />
              <YAxis tick={AXIS} stroke={AXIS.stroke} width={44} />
              <Tooltip content={<ChartTooltip unit="V" />} />
              <ReferenceLine y={0} stroke={GRID} />
              <Line
                type="monotone"
                dataKey="u"
                name="u"
                stroke="#3fb950"
                dot={false}
                strokeWidth={1.5}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      </div>
    </div>
  );
}
