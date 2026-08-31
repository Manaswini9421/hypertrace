import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import App from './App'

// The child screens each do their own fetching; this file is about routing
// and role gating, so they are stubbed to something inert.
vi.mock('./components/Dashboard', () => ({ default: () => <div>cost-screen</div> }))
vi.mock('./components/Incidents', () => ({
  default: ({ canAct }: { canAct: boolean }) => <div>incidents-screen canAct={String(canAct)}</div>,
}))
vi.mock('./components/Policies', () => ({
  default: ({ canAct }: { canAct: boolean }) => <div>policies-screen canAct={String(canAct)}</div>,
}))
vi.mock('./lib/api', () => ({
  auth: {
    role: () => localStorage.getItem('hypertrace.role'),
    clear: () => {
      localStorage.removeItem('hypertrace.token')
      localStorage.removeItem('hypertrace.role')
    },
  },
  login: vi.fn(),
}))

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('when signed out', () => {
  it('shows the login screen instead of any data', () => {
    render(<App />)

    expect(screen.getByPlaceholderText('Username')).toBeInTheDocument()
    expect(screen.queryByText('cost-screen')).not.toBeInTheDocument()
  })
})

describe('when signed in', () => {
  beforeEach(() => {
    localStorage.setItem('hypertrace.role', 'sre')
  })

  it('opens on the cost view', () => {
    render(<App />)

    expect(screen.getByText('cost-screen')).toBeInTheDocument()
  })

  it('switches between screens', async () => {
    render(<App />)

    await userEvent.click(screen.getByRole('button', { name: 'Incidents' }))
    expect(screen.getByText(/incidents-screen/)).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Policies' }))
    expect(screen.getByText(/policies-screen/)).toBeInTheDocument()
  })

  it('shows the current role', () => {
    render(<App />)

    expect(screen.getByText('sre')).toBeInTheDocument()
  })

  it('signing out returns to the login screen and clears the session', async () => {
    render(<App />)

    await userEvent.click(screen.getByRole('button', { name: 'Sign out' }))

    expect(screen.getByPlaceholderText('Username')).toBeInTheDocument()
    expect(localStorage.getItem('hypertrace.role')).toBeNull()
  })
})

describe('role gating', () => {
  it('grants an SRE the ability to act', async () => {
    localStorage.setItem('hypertrace.role', 'sre')
    render(<App />)

    await userEvent.click(screen.getByRole('button', { name: 'Incidents' }))

    expect(screen.getByText(/canAct=true/)).toBeInTheDocument()
  })

  it('withholds it from a finance user', async () => {
    // Cosmetic only — the backend re-checks the role on every write — but
    // it keeps a read-only user from being shown controls that would 403.
    localStorage.setItem('hypertrace.role', 'finance')
    render(<App />)

    await userEvent.click(screen.getByRole('button', { name: 'Incidents' }))

    expect(screen.getByText(/canAct=false/)).toBeInTheDocument()
  })

  it('withholds it from an unrecognised role', async () => {
    localStorage.setItem('hypertrace.role', 'something-else')
    render(<App />)

    await userEvent.click(screen.getByRole('button', { name: 'Policies' }))

    expect(screen.getByText(/canAct=false/)).toBeInTheDocument()
  })

  it('still shows a finance user the cost data they are entitled to', () => {
    localStorage.setItem('hypertrace.role', 'finance')
    render(<App />)

    expect(screen.getByText('cost-screen')).toBeInTheDocument()
  })
})
