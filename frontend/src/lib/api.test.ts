import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, auth, login, openAlertStream } from './api'

/** Minimal unsigned JWT — the client only reads the payload, never verifies it. */
function fakeJwt(payload: Record<string, unknown>): string {
  const encode = (o: unknown) => btoa(JSON.stringify(o)).replace(/=+$/, '')
  return `${encode({ alg: 'HS256' })}.${encode(payload)}.signature`
}

function mockFetch(response: Partial<Response> & { jsonBody?: unknown }) {
  return vi.fn().mockResolvedValue({
    ok: response.ok ?? true,
    status: response.status ?? 200,
    json: async () => response.jsonBody,
    text: async () => JSON.stringify(response.jsonBody ?? ''),
  } as Response)
}

beforeEach(() => {
  localStorage.clear()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('login', () => {
  it('stores the token and the role from its payload', async () => {
    const token = fakeJwt({ sub: 'sre', role: 'sre' })
    vi.stubGlobal('fetch', mockFetch({ jsonBody: { access_token: token } }))

    const role = await login('sre', 'hypertrace-dev')

    expect(role).toBe('sre')
    expect(auth.token()).toBe(token)
    expect(auth.role()).toBe('sre')
  })

  it('reads the finance role so the UI can hide write controls', async () => {
    vi.stubGlobal('fetch', mockFetch({ jsonBody: { access_token: fakeJwt({ sub: 'finance', role: 'finance' }) } }))

    expect(await login('finance', 'hypertrace-dev')).toBe('finance')
  })

  it('rejects bad credentials without storing anything', async () => {
    vi.stubGlobal('fetch', mockFetch({ ok: false, status: 401 }))

    await expect(login('sre', 'wrong')).rejects.toThrow('Invalid username or password')
    expect(auth.token()).toBeNull()
  })

  it('sends credentials as form data, which is what OAuth2PasswordRequestForm expects', async () => {
    const fetchMock = mockFetch({ jsonBody: { access_token: fakeJwt({ sub: 'sre', role: 'sre' }) } })
    vi.stubGlobal('fetch', fetchMock)

    await login('sre', 'hypertrace-dev')

    const [, init] = fetchMock.mock.calls[0]
    expect(init.body).toBeInstanceOf(URLSearchParams)
    expect((init.body as URLSearchParams).get('username')).toBe('sre')
  })
})

describe('authenticated requests', () => {
  it('attaches the bearer token', async () => {
    localStorage.setItem('hypertrace.token', 'tok-123')
    const fetchMock = mockFetch({ jsonBody: [] })
    vi.stubGlobal('fetch', fetchMock)

    await api.anomalies(5)

    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toContain('/api/v1/anomalies?limit=5')
    expect(init.headers.Authorization).toBe('Bearer tok-123')
  })

  it('url-encodes service names so the slash in namespace/workload survives', async () => {
    localStorage.setItem('hypertrace.token', 'tok-123')
    const fetchMock = mockFetch({ jsonBody: [] })
    vi.stubGlobal('fetch', fetchMock)

    await api.serviceCost('hypertrace/victim', 2)

    expect(fetchMock.mock.calls[0][0]).toContain('hypertrace%2Fvictim')
  })

  it('clears the stored session on a 401 rather than looping on a dead token', async () => {
    localStorage.setItem('hypertrace.token', 'expired')
    localStorage.setItem('hypertrace.role', 'sre')
    vi.stubGlobal('fetch', mockFetch({ ok: false, status: 401 }))
    // jsdom refuses navigation; stub it so the 401 path can be observed.
    vi.stubGlobal('location', { reload: vi.fn() })

    await expect(api.anomalies()).rejects.toThrow('Session expired')
    expect(auth.token()).toBeNull()
    expect(auth.role()).toBeNull()
  })

  it('returns nothing for a 204 instead of trying to parse an empty body', async () => {
    localStorage.setItem('hypertrace.token', 'tok')
    vi.stubGlobal('fetch', mockFetch({ status: 204 }))

    await expect(api.deletePolicy('some-id')).resolves.toBeUndefined()
  })

  it('surfaces other errors with their status', async () => {
    localStorage.setItem('hypertrace.token', 'tok')
    vi.stubGlobal('fetch', mockFetch({ ok: false, status: 403, jsonBody: 'forbidden' }))

    await expect(api.createPolicy({ org_id: 'x', rule_dsl: {}, scope: '*', action: 'throttle', priority: 1 }))
      .rejects.toThrow(/403/)
  })
})

describe('auth.clear', () => {
  it('removes both keys so a stale role cannot outlive the token', () => {
    localStorage.setItem('hypertrace.token', 'tok')
    localStorage.setItem('hypertrace.role', 'sre')

    auth.clear()

    expect(auth.token()).toBeNull()
    expect(auth.role()).toBeNull()
  })
})

describe('openAlertStream', () => {
  it('passes the token as a query parameter', () => {
    // The browser WebSocket API cannot set headers on the handshake, so the
    // JWT has to travel in the URL — the server verifies it before upgrading.
    localStorage.setItem('hypertrace.token', 'tok-abc')
    const created: string[] = []
    vi.stubGlobal(
      'WebSocket',
      class {
        constructor(url: string) {
          created.push(url)
        }
        close() {}
      },
    )

    openAlertStream(() => {})

    expect(created[0]).toContain('/api/v1/stream/alerts?token=tok-abc')
  })

  it('parses incoming alerts before handing them to the caller', () => {
    localStorage.setItem('hypertrace.token', 'tok')
    let socket: { onmessage?: (e: { data: string }) => void } = {}
    vi.stubGlobal(
      'WebSocket',
      class {
        onmessage?: (e: { data: string }) => void
        constructor() {
          socket = this
        }
        close() {}
      },
    )
    const received: unknown[] = []

    openAlertStream((a) => received.push(a))
    socket.onmessage?.({ data: JSON.stringify({ id: 'a1', service: 'hypertrace/victim' }) })

    expect(received).toEqual([{ id: 'a1', service: 'hypertrace/victim' }])
  })
})
