// EquityCurveChart — recharts area chart for backtest equity.
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

interface Point {
  date: string;
  equity: number;
}

interface Props {
  data: Point[];
  initialCapital: number;
}

function shortDate(s: string): string {
  const d = new Date(s + "T00:00:00Z");
  if (isNaN(d.getTime())) return s;
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC" });
}

export function EquityCurveChart({ data, initialCapital }: Props) {
  if (!data || data.length === 0) {
    return <p className="empty">No equity curve data.</p>;
  }
  const final = data[data.length - 1]?.equity ?? initialCapital;
  const pct = ((final - initialCapital) / initialCapital) * 100;

  return (
    <div data-testid="equity-curve-chart">
      <div style={{ display: "flex", gap: 16, marginBottom: 8 }}>
        <span>
          Final: <strong className={pct >= 0 ? "pnl-pos" : "pnl-neg"}>
            ₹{final.toLocaleString()} ({pct >= 0 ? "+" : ""}{pct.toFixed(2)}%)
          </strong>
        </span>
      </div>
      <div style={{ width: "100%", height: 240 }}>
        <ResponsiveContainer>
          <AreaChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#3fb950" stopOpacity={0.5} />
                <stop offset="100%" stopColor="#3fb950" stopOpacity={0.05} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="#30363d" strokeDasharray="3 3" />
            <XAxis dataKey="date" tickFormatter={shortDate} stroke="#8b949e" fontSize={11} />
            <YAxis stroke="#8b949e" fontSize={11} width={70} />
            <Tooltip
              contentStyle={{ background: "#161b22", border: "1px solid #30363d" }}
              labelFormatter={(v: string) => shortDate(v)}
              formatter={(v: number) => [`₹${v.toLocaleString()}`, "Equity"]}
            />
            <Area type="monotone" dataKey="equity" stroke="#3fb950" fill="url(#equityFill)" />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
