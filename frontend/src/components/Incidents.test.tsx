import { act, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Action, Anomaly } from '../lib/api'
import Incidents from './Incidents'

vi.mock('../lib/api', () => ({
  api: {
    anomalies: vi.fn(),
    actions: vi.fn(),
    approve: vi.fn(),
    rollback: vi.fn(),
  },
  openAlertStream: vi.fn(),
}))

const { api, openAlertStream } = await import('../lib/api')

function anomaly(id: string, overrides: Partial<Anomaly> = {}): Anomaly {
  return {
    id,
    service: 'hypertrace/victim',
    score: 12.5,
    classification: 'suspected_abuse',
    evidence: {},
    status: 'open',
    created_at: new Date().toISOString(),
    ...overrides,
  }
}

let actionClock = 0

function action(id: string, anomalyId: string, result: string): Action {
  // Each call advances the clock so ledger ordering is deterministic —
  // current state is derived by reading forward, so "executed then rolled
  // back" must be distinguishable from the reverse.
  actionClock += 1000
  return {
    id,
    anomaly_id: anomalyId,
    parent_action_id: null,
    action_type: result === 'rolled_back' ? 'rollback' : 'throttle',
    mode: 'autonomous',
    executed_at: new Date(actionClock).toISOString(),
    result,
    rollback_ref: null,
  }
}

/** Captures the callback so a test can push alerts as the server would. */
let pushAlert: (a: Anomaly) => void
const fakeSocket = { close: vi.fn(), onopen: undefined, onclose: undefined } as unknown as WebSocket

beforeEach(() => {
  vi.mocked(openAlertStream).mockImplementation((cb) => {
    pushAlert = cb
    return fakeSocket
  })
  vi.mocked(api.anomalies).mockResolvedValue([])
  vi.mocked(api.actions).mockResolvedValue([])
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('the incident feed', () => {
  it('renders anomalies with their classification and score', async () => {
    vi.mocked(api.anomalies).mockResolvedValue([anomaly('a1')])

    render(<Incidents canAct />)

    expect(await screen.findByText('suspected_abuse')).toBeInTheDocument()
    expect(screen.getByText(/z = 12.50/)).toBeInTheDocument()
  })

  it('shows the actions attached to an incident', async () => {
    vi.mocked(api.anomalies).mockResolvedValue([anomaly('a1')])
    vi.mocked(api.actions).mockResolvedValue([action('act1', 'a1', 'executed')])

    render(<Incidents canAct />)

    expect(await screen.findByText('throttle → executed')).toBeInTheDocument()
  })

  it('tells the user how to generate one when the feed is empty', async () => {
    render(<Incidents canAct />)

    expect(await screen.findByText(/No anomalies detected/)).toBeInTheDocument()
  })
})

describe('the Needs action filter (bug 7)', () => {
  // A quiet cluster emits a steady trickle of low-value anomalies. The
  // incident that actually triggered remediation had sunk to rank 223 while
  // the feed loaded only the newest 50, so its Roll back button was never
  // rendered and the control was unreachable.
  const noise = Array.from({ length: 60 }, (_, i) => anomaly(`noise-${i}`, { classification: 'misconfiguration_or_waste' }))
  const actionable = anomaly('acted', { classification: 'likely_bug_from_deployment' })

  beforeEach(() => {
    vi.mocked(api.anomalies).mockResolvedValue([...noise, actionable])
    vi.mocked(api.actions).mockResolvedValue([action('act1', 'acted', 'executed')])
  })

  it('hides incidents that never triggered an action', async () => {
    render(<Incidents canAct />)
    await screen.findAllByText('misconfiguration_or_waste')

    await userEvent.click(screen.getByRole('button', { name: 'Needs action' }))

    await waitFor(() => expect(screen.queryByText('misconfiguration_or_waste')).not.toBeInTheDocument())
    expect(screen.getByText('likely_bug_from_deployment')).toBeInTheDocument()
  })

  it('fetches a deeper window so buried incidents are reachable', async () => {
    render(<Incidents canAct />)
    await screen.findAllByText('misconfiguration_or_waste')

    await userEvent.click(screen.getByRole('button', { name: 'Needs action' }))

    await waitFor(() => expect(vi.mocked(api.anomalies)).toHaveBeenCalledWith(500))
  })

  it('keeps the actionable incident visible when a live alert arrives', async () => {
    // The sub-bug: the WebSocket handler capped the list at 50 on every
    // incoming alert, truncating the deeper fetch moments after it loaded
    // and making the Roll back button vanish again.
    //
    // The alert is pushed inside act() so React flushes the state update
    // before the assertion. Without that, `waitFor(... toBeInTheDocument)`
    // passes on its first check — against the *pre-update* DOM — and the
    // test silently proves nothing. (Verified: an earlier version of this
    // test passed against the reverted fix.)
    //
    // The actionable incident is deliberately last in the fetched list, so
    // a 50-item cap drops it while the 500-item cap keeps it.
    render(<Incidents canAct />)
    await screen.findAllByText('misconfiguration_or_waste')
    await userEvent.click(screen.getByRole('button', { name: 'Needs action' }))
    expect(await screen.findByRole('button', { name: 'Roll back' })).toBeInTheDocument()

    await act(async () => {
      pushAlert(anomaly('live-1', { classification: 'misconfiguration_or_waste' }))
    })

    expect(screen.getByRole('button', { name: 'Roll back' })).toBeInTheDocument()
  })

  it('explains an empty result rather than blaming the user', async () => {
    vi.mocked(api.actions).mockResolvedValue([])
    render(<Incidents canAct />)
    await screen.findAllByText('misconfiguration_or_waste')

    await userEvent.click(screen.getByRole('button', { name: 'Needs action' }))

    expect(await screen.findByText(/No incidents have triggered a remediation action/)).toBeInTheDocument()
  })
})

describe('remediation controls', () => {
  it('offers Roll back only for an action that executed', async () => {
    vi.mocked(api.anomalies).mockResolvedValue([anomaly('a1')])
    vi.mocked(api.actions).mockResolvedValue([action('act1', 'a1', 'executed')])

    render(<Incidents canAct />)

    expect(await screen.findByRole('button', { name: 'Roll back' })).toBeInTheDocument()
  })

  it('offers no Roll back for an action that was only a no-op', async () => {
    vi.mocked(api.anomalies).mockResolvedValue([anomaly('a1')])
    vi.mocked(api.actions).mockResolvedValue([action('act1', 'a1', 'no_op')])

    render(<Incidents canAct />)
    await screen.findByText('throttle → no_op')

    expect(screen.queryByRole('button', { name: 'Roll back' })).not.toBeInTheDocument()
  })

  it('offers Approve for an action awaiting a human', async () => {
    vi.mocked(api.anomalies).mockResolvedValue([anomaly('a1')])
    vi.mocked(api.actions).mockResolvedValue([action('act1', 'a1', 'pending_approval')])

    render(<Incidents canAct />)

    expect(await screen.findByRole('button', { name: 'Approve' })).toBeInTheDocument()
  })

  it('withdraws Roll back once the action has already been rolled back', async () => {
    // Reading the ledger forward: an executed action followed by a
    // rolled_back one is no longer rollbackable. Checking only "is there an
    // executed row" would offer to undo something already undone.
    vi.mocked(api.anomalies).mockResolvedValue([anomaly('a1')])
    vi.mocked(api.actions).mockResolvedValue([
      action('act1', 'a1', 'executed'),
      action('act2', 'a1', 'rolled_back'),
    ])

    render(<Incidents canAct />)
    await screen.findByText('rollback → rolled_back')

    expect(screen.queryByRole('button', { name: 'Roll back' })).not.toBeInTheDocument()
  })

  it('withdraws Approve once the action has been dispatched', async () => {
    vi.mocked(api.anomalies).mockResolvedValue([anomaly('a1')])
    vi.mocked(api.actions).mockResolvedValue([
      action('act1', 'a1', 'pending_approval'),
      action('act2', 'a1', 'dispatched'),
    ])

    render(<Incidents canAct />)
    await screen.findByText('throttle → dispatched')

    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument()
  })

  it('hides every control from a read-only user', async () => {
    // The backend enforces this independently; hiding the button is so a
    // finance user is not shown controls that would 403.
    vi.mocked(api.anomalies).mockResolvedValue([anomaly('a1')])
    vi.mocked(api.actions).mockResolvedValue([action('act1', 'a1', 'executed')])

    render(<Incidents canAct={false} />)
    await screen.findByText('throttle → executed')

    expect(screen.queryByRole('button', { name: 'Roll back' })).not.toBeInTheDocument()
  })

  it('calls rollback and reports the outcome', async () => {
    vi.mocked(api.anomalies).mockResolvedValue([anomaly('a1')])
    vi.mocked(api.actions).mockResolvedValue([action('act1', 'a1', 'executed')])
    vi.mocked(api.rollback).mockResolvedValue({ action_id: 'abcdef1234', status: 'dispatched' })

    render(<Incidents canAct />)
    await userEvent.click(await screen.findByRole('button', { name: 'Roll back' }))

    expect(vi.mocked(api.rollback)).toHaveBeenCalledWith('a1')
    expect(await screen.findByText(/rollback dispatched/)).toBeInTheDocument()
  })

  it('surfaces a failed rollback instead of silently doing nothing', async () => {
    vi.mocked(api.anomalies).mockResolvedValue([anomaly('a1')])
    vi.mocked(api.actions).mockResolvedValue([action('act1', 'a1', 'executed')])
    vi.mocked(api.rollback).mockRejectedValue(new Error('503 queue unreachable'))

    render(<Incidents canAct />)
    await userEvent.click(await screen.findByRole('button', { name: 'Roll back' }))

    expect(await screen.findByText(/503 queue unreachable/)).toBeInTheDocument()
  })
})

describe('the live stream indicator', () => {
  it('starts disconnected until the socket opens', async () => {
    render(<Incidents canAct />)

    expect(await screen.findByText('Alert stream disconnected')).toBeInTheDocument()
  })

  it('does not duplicate an alert the poll already returned', async () => {
    vi.mocked(api.anomalies).mockResolvedValue([anomaly('a1')])
    render(<Incidents canAct />)
    await screen.findByText('suspected_abuse')

    await act(async () => {
      pushAlert(anomaly('a1'))
    })

    expect(screen.getAllByText('suspected_abuse')).toHaveLength(1)
  })
})
