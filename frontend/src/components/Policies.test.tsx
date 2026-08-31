import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Policy } from '../lib/api'
import Policies from './Policies'

vi.mock('../lib/api', () => ({
  api: { policies: vi.fn(), createPolicy: vi.fn(), deletePolicy: vi.fn() },
}))

const { api } = await import('../lib/api')

const existing: Policy = {
  id: 'p1',
  org_id: 'default',
  rule_dsl: { service_prefix: 'hypertrace/victim', min_cost_per_hour: 0.01 },
  scope: '*',
  action: 'throttle',
  priority: 100,
}

beforeEach(() => {
  vi.mocked(api.policies).mockResolvedValue([])
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('listing policies', () => {
  it('shows a policy with its action and rule', async () => {
    vi.mocked(api.policies).mockResolvedValue([existing])

    render(<Policies canAct />)

    expect(await screen.findByText('throttle')).toBeInTheDocument()
    expect(screen.getByText(/hypertrace\/victim/)).toBeInTheDocument()
  })

  it('says so plainly when there are none', async () => {
    // Worth stating outright: with no policies, anomalies are alert-only.
    render(<Policies canAct />)

    expect(await screen.findByText(/anomalies will alert only/)).toBeInTheDocument()
  })
})

describe('role gating', () => {
  it('offers no form to a read-only user', async () => {
    vi.mocked(api.policies).mockResolvedValue([existing])

    render(<Policies canAct={false} />)
    await screen.findByText('throttle')

    expect(screen.getByText(/read-only/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Create policy' })).not.toBeInTheDocument()
  })

  it('offers no delete control to a read-only user', async () => {
    vi.mocked(api.policies).mockResolvedValue([existing])

    render(<Policies canAct={false} />)
    await screen.findByText('throttle')

    expect(screen.queryByText('delete')).not.toBeInTheDocument()
  })

  it('gives an SRE the full form', async () => {
    render(<Policies canAct />)

    expect(await screen.findByRole('button', { name: 'Create policy' })).toBeInTheDocument()
  })
})

describe('creating a policy', () => {
  it('builds the rule from the form fields', async () => {
    vi.mocked(api.createPolicy).mockResolvedValue({ ...existing, id: 'new' })
    render(<Policies canAct />)
    await screen.findByRole('button', { name: 'Create policy' })

    await userEvent.click(screen.getByRole('button', { name: 'Create policy' }))

    await waitFor(() =>
      expect(vi.mocked(api.createPolicy)).toHaveBeenCalledWith(
        expect.objectContaining({
          action: 'throttle',
          priority: 100,
          rule_dsl: { service_prefix: 'hypertrace/', min_cost_per_hour: 0.01 },
        }),
      ),
    )
  })

  it('marks a policy as needing approval when the box is ticked', async () => {
    // This is the semi-autonomous mode from doc 11.1 — the mitigation for
    // acting on a false positive — so the flag must actually reach the API.
    vi.mocked(api.createPolicy).mockResolvedValue({ ...existing, id: 'new' })
    render(<Policies canAct />)
    await screen.findByRole('button', { name: 'Create policy' })

    await userEvent.click(screen.getByRole('checkbox'))
    await userEvent.click(screen.getByRole('button', { name: 'Create policy' }))

    await waitFor(() =>
      expect(vi.mocked(api.createPolicy)).toHaveBeenCalledWith(
        expect.objectContaining({ rule_dsl: expect.objectContaining({ requires_approval: true }) }),
      ),
    )
  })

  it('omits an empty prefix rather than sending a rule that matches nothing', async () => {
    vi.mocked(api.createPolicy).mockResolvedValue({ ...existing, id: 'new' })
    render(<Policies canAct />)
    const prefix = await screen.findByPlaceholderText('hypertrace/victim')

    await userEvent.clear(prefix)
    await userEvent.click(screen.getByRole('button', { name: 'Create policy' }))

    const sent = vi.mocked(api.createPolicy).mock.calls[0][0]
    expect(sent.rule_dsl).not.toHaveProperty('service_prefix')
  })

  it('shows the error when the API refuses', async () => {
    vi.mocked(api.createPolicy).mockRejectedValue(new Error('403 Forbidden'))
    render(<Policies canAct />)
    await screen.findByRole('button', { name: 'Create policy' })

    await userEvent.click(screen.getByRole('button', { name: 'Create policy' }))

    expect(await screen.findByText(/403 Forbidden/)).toBeInTheDocument()
  })

  it('refreshes the list so the new policy appears', async () => {
    vi.mocked(api.createPolicy).mockResolvedValue({ ...existing, id: 'new' })
    render(<Policies canAct />)
    await screen.findByRole('button', { name: 'Create policy' })

    await userEvent.click(screen.getByRole('button', { name: 'Create policy' }))

    await waitFor(() => expect(vi.mocked(api.policies).mock.calls.length).toBeGreaterThan(1))
  })
})

describe('deleting a policy', () => {
  it('calls the API and refreshes', async () => {
    vi.mocked(api.policies).mockResolvedValue([existing])
    vi.mocked(api.deletePolicy).mockResolvedValue(undefined)
    render(<Policies canAct />)

    await userEvent.click(await screen.findByText('delete'))

    expect(vi.mocked(api.deletePolicy)).toHaveBeenCalledWith('p1')
  })
})
