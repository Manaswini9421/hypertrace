import { useEffect, useState } from 'react'
import { api, openAlertStream, type Action, type Anomaly } from '../lib/api'

const CLASSIFICATION_STYLES: Record<string, string> = {
  suspected_abuse: 'bg-red-500/15 text-red-300',
  likely_bug_from_deployment: 'bg-amber-500/15 text-amber-300',
  misconfiguration_or_waste: 'bg-sky-500/15 text-sky-300',
  legitimate_traffic_growth: 'bg-emerald-500/15 text-emerald-300',
  unclassified: 'bg-slate-500/15 text-slate-300',
}

// A quiet cluster produces a steady trickle of low-value anomalies (normal
// jitter crossing 3σ on a very stable baseline), and they bury the incidents
// that actually triggered remediation — the ones an SRE needs to reach. The
// "Needs action" view fetches a deeper window and keeps only anomalies with
// an action attached, so the triage view stays usable as history grows.
const RECENT_WINDOW = 50
const ACTIONABLE_WINDOW = 500

export default function Incidents({ canAct }: { canAct: boolean }) {
  const [anomalies, setAnomalies] = useState<Anomaly[]>([])
  const [actions, setActions] = useState<Action[]>([])
  const [live, setLive] = useState(false)
  const [busy, setBusy] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [actionableOnly, setActionableOnly] = useState(false)

  async function refresh(onlyActionable = actionableOnly) {
    const [a, act] = await Promise.all([
      api.anomalies(onlyActionable ? ACTIONABLE_WINDOW : RECENT_WINDOW),
      api.actions(200),
    ])
    setAnomalies(a)
    setActions(act)
  }

  useEffect(() => {
    refresh().catch(() => {})
    const id = setInterval(() => refresh().catch(() => {}), 10000)
    return () => clearInterval(id)
    // Re-runs on toggle so the deeper fetch happens immediately, not on the
    // next 10s tick.
  }, [actionableOnly])

  useEffect(() => {
    const ws = openAlertStream((alert) => {
      // Prepend the live alert, guarding against a duplicate if the polling
      // refresh already picked the same anomaly up.
      //
      // Cap at the widest window, not the "Recent" one: the deeper fetch
      // behind "Needs action" would otherwise be truncated back to 50 by the
      // next incoming alert, silently hiding the actionable incidents it was
      // loaded to surface.
      setAnomalies((prev) =>
        prev.some((a) => a.id === alert.id) ? prev : [alert, ...prev].slice(0, ACTIONABLE_WINDOW),
      )
    })
    ws.onopen = () => setLive(true)
    ws.onclose = () => setLive(false)
    return () => ws.close()
  }, [])

  async function act(kind: 'approve' | 'rollback', anomalyId: string) {
    setBusy(anomalyId)
    setNote('')
    try {
      const res = kind === 'approve' ? await api.approve(anomalyId) : await api.rollback(anomalyId)
      setNote(`${kind} ${res.status} (action ${res.action_id.slice(0, 8)})`)
      await refresh()
    } catch (e) {
      setNote(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(null)
    }
  }

  const actionsByAnomaly = new Map<string, Action[]>()
  for (const a of actions) {
    if (!a.anomaly_id) continue
    const list = actionsByAnomaly.get(a.anomaly_id) ?? []
    list.push(a)
    actionsByAnomaly.set(a.anomaly_id, list)
  }

  const visible = actionableOnly ? anomalies.filter((a) => actionsByAnomaly.has(a.id)) : anomalies

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 text-sm">
        <span className={`h-2 w-2 rounded-full ${live ? 'bg-emerald-400' : 'bg-slate-600'}`} />
        <span className="text-slate-400">{live ? 'Live alert stream connected' : 'Alert stream disconnected'}</span>
        <div className="ml-auto flex gap-1">
          {[
            { id: false, label: 'Recent' },
            { id: true, label: 'Needs action' },
          ].map((opt) => (
            <button
              key={String(opt.id)}
              onClick={() => setActionableOnly(opt.id)}
              className={`rounded px-2 py-0.5 text-xs ${
                actionableOnly === opt.id ? 'bg-slate-700 text-slate-100' : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>
      {note && <p className="text-xs text-sky-300">{note}</p>}

      {visible.length === 0 ? (
        <p className="rounded-lg border border-slate-800 bg-slate-900 p-6 text-sm text-slate-400">
          {actionableOnly
            ? 'No incidents have triggered a remediation action yet.'
            : 'No anomalies detected. Trigger one with ./services/workload-simulator/trigger.sh runaway-retry.'}
        </p>
      ) : (
        <ul className="space-y-2">
          {visible.map((a) => {
            const related = actionsByAnomaly.get(a.id) ?? []
            const hasExecuted = related.some((r) => r.result === 'executed')
            const isPending = related.some((r) => r.result === 'pending_approval')
            return (
              <li key={a.id} className="rounded-lg border border-slate-800 bg-slate-900 p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <span className={`rounded px-2 py-0.5 text-xs ${CLASSIFICATION_STYLES[a.classification] ?? ''}`}>
                    {a.classification}
                  </span>
                  <span className="font-mono text-xs text-slate-300">{a.service}</span>
                  <span className="text-xs text-slate-500">z = {a.score.toFixed(2)}</span>
                  <span className="ml-auto text-xs text-slate-500">
                    {new Date(a.created_at).toLocaleTimeString()}
                  </span>
                </div>

                {related.length > 0 && (
                  <div className="mt-2 flex flex-wrap gap-1">
                    {related.map((r) => (
                      <span key={r.id} className="rounded bg-slate-800 px-2 py-0.5 font-mono text-[11px] text-slate-400">
                        {r.action_type} → {r.result}
                      </span>
                    ))}
                  </div>
                )}

                {canAct && (isPending || hasExecuted) && (
                  <div className="mt-3 flex gap-2">
                    {isPending && (
                      <button
                        onClick={() => act('approve', a.id)}
                        disabled={busy === a.id}
                        className="rounded bg-emerald-600 px-3 py-1 text-xs hover:bg-emerald-500 disabled:opacity-50"
                      >
                        Approve
                      </button>
                    )}
                    {hasExecuted && (
                      <button
                        onClick={() => act('rollback', a.id)}
                        disabled={busy === a.id}
                        className="rounded bg-slate-700 px-3 py-1 text-xs hover:bg-slate-600 disabled:opacity-50"
                      >
                        Roll back
                      </button>
                    )}
                  </div>
                )}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}
