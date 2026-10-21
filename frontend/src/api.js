// Portal API client. The session lives in a cookie; mutating requests carry a CSRF header.
import { translate } from './i18n/index.jsx'

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message)
    this.status = status
    this.code = code
  }
}

async function request(method, path, body) {
  const headers = { Accept: 'application/json' }
  if (method !== 'GET') headers['X-SF-CSRF'] = '1'
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  let res
  try {
    res = await fetch(path, { method, headers, credentials: 'same-origin', body: body === undefined ? undefined : JSON.stringify(body) })
  } catch {
    throw new ApiError(0, 'network', translate('api.network'))
  }
  let data = null
  try { data = await res.json() } catch { /* empty body */ }
  if (!res.ok) {
    const e = data?.error
    throw new ApiError(res.status, e?.code || 'error', e?.message || translate('api.status', { status: res.status }))
  }
  return data
}

export const api = {
  get: (p) => request('GET', p),
  post: (p, b = {}) => request('POST', p, b),
  patch: (p, b = {}) => request('PATCH', p, b),
  put: (p, b = {}) => request('PUT', p, b),
}

export const qs = (o) => {
  const p = new URLSearchParams()
  for (const [k, v] of Object.entries(o)) if (v !== undefined && v !== null && v !== '' && v !== false) p.set(k, v)
  const s = p.toString()
  return s ? `?${s}` : ''
}
