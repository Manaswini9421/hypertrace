import { useState } from 'react'
import Dashboard from './components/Dashboard'
import Incidents from './components/Incidents'
import Login from './components/Login'
import Policies from './components/Policies'
import { auth } from './lib/api'

type Tab = 'dashboard' | 'incidents' | 'policies'

export default function App() {
  const [role, setRole] = useState<string | null>(auth.role())
  const [tab, setTab] = useState<Tab>('dashboard')

  if (!role) return <Login onLogin={setRole} />

  // Remediation and policy writes are SRE-only. The backend enforces this
  // independently on every request (see require_sre) — hiding controls here
  // is purely so a finance user isn't shown buttons that would 403.
  const canAct = role === 'sre'

  const tabs: { id: Tab; label: string }[] = [
    { id: 'dashboard', label: 'Cost' },
    { id: 'incidents', label: 'Incidents' },
    { id: 'policies', label: 'Policies' },
  ]

  return (
    <div className="mx-auto max-w-6xl p-6">
      <header className="mb-6 flex items-center gap-4">
        <h1 className="text-lg font-semibold">HyperTrace</h1>
        <nav className="flex gap-1">
          {tabs.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`rounded px-3 py-1.5 text-sm ${
                tab === t.id ? 'bg-slate-800 text-slate-100' : 'text-slate-400 hover:text-slate-200'
              }`}
            >
              {t.label}
            </button>
          ))}
        </nav>
        <div className="ml-auto flex items-center gap-3 text-sm">
          <span className="rounded bg-slate-800 px-2 py-0.5 text-xs text-slate-300">{role}</span>
          <button
            onClick={() => {
              auth.clear()
              setRole(null)
            }}
            className="text-slate-400 hover:text-slate-200"
          >
            Sign out
          </button>
        </div>
      </header>

      {tab === 'dashboard' && <Dashboard />}
      {tab === 'incidents' && <Incidents canAct={canAct} />}
      {tab === 'policies' && <Policies canAct={canAct} />}
    </div>
  )
}
