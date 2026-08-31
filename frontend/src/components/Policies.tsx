import { useEffect, useState } from 'react'
import { api, type Policy } from '../lib/api'

const ACTIONS = ['alert_only', 'throttle', 'freeze_scaling'] as const

export default function Policies({ canAct }: { canAct: boolean }) {
  const [policies, setPolicies] = useState<Policy[]>([])
  const [servicePrefix, setServicePrefix] = useState('hypertrace/')
  const [minCost, setMinCost] = useState('0.01')
  const [action, setAction] = useState<string>('throttle')
  const [priority, setPriority] = useState('100')
  const [requiresApproval, setRequiresApproval] = useState(false)
  const [error, setError] = useState('')

  const refresh = () => api.policies().then(setPolicies).catch((e) => setError(String(e)))
  useEffect(() => {
    refresh()
  }, [])

  async function create(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    try {
      const rule_dsl: Record<string, unknown> = {}
      if (servicePrefix.trim()) rule_dsl.service_prefix = servicePrefix.trim()
      if (minCost.trim()) rule_dsl.min_cost_per_hour = Number(minCost)
      if (requiresApproval) rule_dsl.requires_approval = true
      await api.createPolicy({ org_id: 'default', scope: '*', action, priority: Number(priority), rule_dsl })
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  async function remove(id: string) {
    setError('')
    try {
      await api.deletePolicy(id)
      await refresh()
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    }
  }

  return (
    <div className="grid gap-6 lg:grid-cols-2">
      <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">Active policies</h2>
        {policies.length === 0 ? (
          <p className="text-sm text-slate-500">No policies defined — anomalies will alert only.</p>
        ) : (
          <ul className="space-y-2">
            {policies.map((p) => (
              <li key={p.id} className="rounded border border-slate-800 bg-slate-950 p-3">
                <div className="flex items-center gap-2">
                  <span className="rounded bg-sky-500/15 px-2 py-0.5 text-xs text-sky-300">{p.action}</span>
                  <span className="text-xs text-slate-500">priority {p.priority}</span>
                  {canAct && (
                    <button
                      onClick={() => remove(p.id)}
                      className="ml-auto text-xs text-slate-500 hover:text-red-400"
                    >
                      delete
                    </button>
                  )}
                </div>
                <pre className="mt-2 overflow-x-auto text-[11px] text-slate-400">
                  {JSON.stringify(p.rule_dsl, null, 2)}
                </pre>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="rounded-lg border border-slate-800 bg-slate-900 p-4">
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-slate-400">New policy</h2>
        {!canAct ? (
          <p className="text-sm text-slate-500">Your role is read-only. Sign in as an SRE to define policies.</p>
        ) : (
          <form onSubmit={create} className="space-y-3 text-sm">
            <label className="block">
              <span className="text-xs text-slate-400">Service prefix</span>
              <input
                className="mt-1 w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5"
                value={servicePrefix}
                onChange={(e) => setServicePrefix(e.target.value)}
                placeholder="hypertrace/victim"
              />
            </label>
            <label className="block">
              <span className="text-xs text-slate-400">Minimum cost/hour to trigger</span>
              <input
                className="mt-1 w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5"
                value={minCost}
                onChange={(e) => setMinCost(e.target.value)}
              />
            </label>
            <label className="block">
              <span className="text-xs text-slate-400">Action</span>
              <select
                className="mt-1 w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5"
                value={action}
                onChange={(e) => setAction(e.target.value)}
              >
                {ACTIONS.map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
            </label>
            <label className="block">
              <span className="text-xs text-slate-400">Priority</span>
              <input
                className="mt-1 w-full rounded border border-slate-700 bg-slate-800 px-3 py-1.5"
                value={priority}
                onChange={(e) => setPriority(e.target.value)}
              />
            </label>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={requiresApproval}
                onChange={(e) => setRequiresApproval(e.target.checked)}
              />
              <span className="text-xs text-slate-400">
                Require human approval before acting (recommend-only mode)
              </span>
            </label>
            {error && <p className="text-xs text-red-400">{error}</p>}
            <button type="submit" className="w-full rounded bg-sky-600 py-2 text-sm hover:bg-sky-500">
              Create policy
            </button>
          </form>
        )}
      </div>
    </div>
  )
}
