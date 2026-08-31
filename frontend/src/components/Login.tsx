import { useState } from 'react'
import { login } from '../lib/api'

export default function Login({ onLogin }: { onLogin: (role: string) => void }) {
  const [username, setUsername] = useState('sre')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      onLogin(await login(username, password))
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center">
      <form onSubmit={submit} className="w-80 space-y-4 rounded-lg border border-slate-800 bg-slate-900 p-6">
        <div>
          <h1 className="text-xl font-semibold">HyperTrace</h1>
          <p className="text-sm text-slate-400">Cloud Cost Intelligence &amp; Protection</p>
        </div>
        <input
          className="w-full rounded border border-slate-700 bg-slate-800 px-3 py-2 text-sm"
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          placeholder="Username"
          autoComplete="username"
        />
        <input
          className="w-full rounded border border-slate-700 bg-slate-800 px-3 py-2 text-sm"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          placeholder="Password"
          autoComplete="current-password"
        />
        {error && <p className="text-sm text-red-400">{error}</p>}
        <button
          type="submit"
          disabled={busy}
          className="w-full rounded bg-sky-600 py-2 text-sm font-medium hover:bg-sky-500 disabled:opacity-50"
        >
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
        <p className="text-xs text-slate-500">
          Demo accounts: <code>sre</code> (full access) or <code>finance</code> (read-only).
        </p>
      </form>
    </div>
  )
}
