import { useEffect, useState } from 'react'
import { CartesianGrid, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, type CostPoint, type DashboardSummary } from '../lib/api'

const money = (n: number) => `$${n.toFixed(6)}`

export default function Dashboard() {
  const [summary, setSummary] = useState<DashboardSummary | null>(null)
  const [selected, setSelected] = useState<string | null>(null)
  const [series, setSeries] = useState<CostPoint[]>([])
  const [error, setError] = useState('')

  useEffect(() => {
    const tick = () => api.summary().then(setSummary).catch((e) => setError(String(e)))
    tick()
    const id = setInterval(tick, 5000)
    return () => clearInterval(id)
  }, [])

  // Default to whichever service currently costs the most, so the deep-dive
  // panel is never empty on first load.
  useEffect(() => {
    if (!selected && summary?.top_services.length) setSelected(summary.top_services[0].service)
  }, [summary, selected])

  useEffect(() => {
    if (!selected) return
    const tick = () => api.serviceCost(selected, 1).then(setSeries).catch(() => {})
    tick()
    const id = setInterval(tick, 5000)
    return () => clearInterval(id)
  }, [selected])

  if (error) return <p className="text-red-400">{error}</p>
  if (!summary) return <p className="text-slate-400">Loading…</p>

  const chartData = series.map((p) => ({
    t: new Date(p.time).toLocaleTimeString(),
    cost: p.cost_per_hour,
  }))

  return (
    <div className="space-y-6">
      <div className="rounded-lg border border-slate-800 bg-slate-900 p-6">
        <p className="text-sm uppercase tracking-wide text-slate-400">Live cluster spend</p>
        <p className="mt-1 text-4xl font-semibold tabular-nums text-sky-400">
          {money(summary.total_cost_per_hour)}
          <span className="ml-2 text-lg text-slate-500">/hour</span>
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">
            Most expensive services
          </h2>
          <ul className="space-y-1">
            {summary.top_services.map((s) => (
              <li key={s.service}>
                <button
                  onClick={() => setSelected(s.service)}
                  className={`flex w-full items-center justify-between rounded px-2 py-1.5 text-left text-sm hover:bg-slate-800 ${
                    selected === s.service ? 'bg-slate-800' : ''
                  }`}
                >
                  <span className="truncate font-mono text-xs">{s.service}</span>
                  <span className="ml-3 shrink-0 tabular-nums text-slate-300">{money(s.cost_per_hour)}</span>
                </button>
              </li>
            ))}
          </ul>
        </div>

        <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
          <h2 className="mb-1 text-sm font-semibold uppercase tracking-wide text-slate-400">Service deep-dive</h2>
          <p className="mb-3 truncate font-mono text-xs text-slate-500">{selected ?? '—'}</p>
          <div className="h-56">
            {chartData.length === 0 ? (
              <p className="pt-16 text-center text-sm text-slate-500">No cost data yet for this service.</p>
            ) : (
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={chartData}>
                  <CartesianGrid stroke="#1e293b" />
                  <XAxis dataKey="t" tick={{ fontSize: 10, fill: '#64748b' }} minTickGap={40} />
                  <YAxis tick={{ fontSize: 10, fill: '#64748b' }} width={70} />
                  <Tooltip
                    contentStyle={{ background: '#0f172a', border: '1px solid #1e293b', fontSize: 12 }}
                    formatter={(v: number) => money(v)}
                  />
                  <Line type="monotone" dataKey="cost" stroke="#38bdf8" dot={false} strokeWidth={2} />
                </LineChart>
              </ResponsiveContainer>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}
