// Keeps the referral code until sign-up.
//
// `?ref=CODE` is captured on any page and stored in two places:
//   * `sf_ref` cookie — the backend reads it itself both on password sign-up and when
//     Google/Telegram return (`/portal/auth/*/callback`), so it survives redirects;
//   * localStorage — if the cookie is lost (expiry, browser cleanup) it is restored on the next visit.
// The backend deletes the cookie once the account is created; for signed-in users localStorage is cleared.
//
// `sf_dev` — a device marker (random, 1 year): lets the admin see many accounts opened from one device.

const REF = 'sf_ref'
const DEV = 'sf_dev'
const CODE_RE = /^[A-Za-z0-9]{4,16}$/
const REF_DAYS = 30

function readCookie(name) {
  const m = document.cookie.match(new RegExp(`(?:^|; )${name}=([^;]*)`))
  return m ? decodeURIComponent(m[1]) : ''
}

function writeCookie(name, value, days) {
  const secure = window.location.protocol === 'https:' ? '; Secure' : ''
  document.cookie = `${name}=${encodeURIComponent(value)}; Max-Age=${days * 86400}; Path=/; SameSite=Lax${secure}`
}

function store(fn) {
  try { return fn(window.localStorage) } catch { return null }   // private mode — localStorage may be unavailable
}

function randomId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID()
  const a = new Uint8Array(16)
  window.crypto.getRandomValues(a)
  return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('')
}

/** Called once when the app starts. */
export function captureReferral() {
  try {
    const fromUrl = new URLSearchParams(window.location.search).get('ref') || ''
    if (CODE_RE.test(fromUrl)) {
      const code = fromUrl.toUpperCase()
      store((s) => s.setItem(REF, code))
      writeCookie(REF, code, REF_DAYS)
    } else if (!readCookie(REF)) {
      const saved = store((s) => s.getItem(REF))
      if (saved && CODE_RE.test(saved)) writeCookie(REF, saved, REF_DAYS)
    }
    if (!readCookie(DEV)) writeCookie(DEV, randomId(), 365)
  } catch { /* cookies disabled — referrals simply don't work */ }
}

/** A signed-in user no longer needs the code (linking only happens on sign-up). */
export function forgetReferral() {
  store((s) => s.removeItem(REF))
  if (readCookie(REF)) writeCookie(REF, '', 0)
}
