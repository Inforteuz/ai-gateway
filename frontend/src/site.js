// Platform name and URLs. Never hard-coded: taken from the `site` field of the
// `/portal/auth/me` response, with the defaults below when it is missing.
export const DEFAULT_BRAND = 'AI Gateway'

// Placeholder shown in examples when no model is available
export const MODEL_PLACEHOLDER = 'your-model-id'

const trimSlash = (s) => String(s || '').trim().replace(/\/+$/, '')

/** Normalizes the server-provided (or missing) `site` object, filling in defaults. */
export function normalizeSite(raw) {
  const r = raw && typeof raw === 'object' ? raw : {}
  const origin = typeof window !== 'undefined' ? window.location.origin : ''
  const brand = (r.brand_name || '').trim() || DEFAULT_BRAND
  return {
    brand_name: brand,
    assistant_name: (r.assistant_name || '').trim() || brand,
    site_url: trimSlash(r.site_url) || origin,
    api_url: trimSlash(r.api_url) || `${origin}/v1`,
    // UI language used until the viewer picks one ("uz" | "ru" | "en")
    default_lang: (r.default_lang || '').trim().toLowerCase() || 'uz',
  }
}

const LOCAL_HOSTS = ['localhost', '127.0.0.1', '[::1]']

/** API origin (without the trailing /v1). In local development the current origin is used. */
export function apiOrigin(site) {
  if (typeof window !== 'undefined' && LOCAL_HOSTS.includes(window.location.hostname)) return window.location.origin
  return trimSlash(site?.api_url).replace(/\/v1$/, '') || (typeof window !== 'undefined' ? window.location.origin : '')
}

/** Strips the scheme from a URL: "https://api.example.com/v1" -> "api.example.com/v1". */
export const hostOf = (url) => String(url || '').replace(/^[a-z]+:\/\//i, '')

/**
 * Model id for examples: the first model with capability `cap`, otherwise the
 * first model, or the placeholder when the list is empty.
 */
export function pickModel(items, cap) {
  const list = Array.isArray(items) ? items : []
  const hit = cap ? list.find((m) => m?.capabilities?.[cap]) : null
  return (hit || list[0])?.id || MODEL_PLACEHOLDER
}

/** Models with reasoning enabled. */
export const reasoningModels = (items) =>
  (Array.isArray(items) ? items : []).filter((m) => m?.capabilities?.reasoning_levels?.length > 0)

/** A free (zero-priced) model, or null. */
export const freeModel = (items) =>
  (Array.isArray(items) ? items : []).find((m) => !m?.price_in_per_1m && !m?.price_out_per_1m) || null

// Name of the extra metadata field in API responses (backend format, e.g.
// `"<META_FIELD>": {"verified": true}`). Update here if the backend renames it.
export const META_FIELD = 'gateway'
