// Google reCAPTCHA v3 (bot protection for sign-in, sign-up and password reset).
//
// v3 is invisible: no widget; on submit `grecaptcha.execute()` returns a token,
// the server verifies it with Google and compares the score to a threshold.
// The server's `/portal/auth/providers` response carries
// `captcha: { site_key, login_after_fails }`; without configured keys it is
// `null` and the script is never loaded.
//
// Google's corner badge is hidden with CSS — Google only allows that when the
// <CaptchaNotice /> text is shown next to the form.
//
// CSP: the script comes from www.google.com / www.gstatic.com and the inner
// iframe from www.google.com — allowed in deploy/security-headers.conf.
import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { getLang, translate, useT } from '../i18n/index.jsx'

let scriptPromise = null
let scriptKey = null
function loadRecaptcha(siteKey) {
  if (!scriptPromise || scriptKey !== siteKey) {
    scriptKey = siteKey
    scriptPromise = new Promise((resolve, reject) => {
      const s = document.createElement('script')
      s.src = `https://www.google.com/recaptcha/api.js?render=${encodeURIComponent(siteKey)}&hl=${getLang()}`
      s.async = true
      s.defer = true
      s.onload = () => {
        if (window.grecaptcha?.ready) window.grecaptcha.ready(() => resolve(window.grecaptcha))
        else reject(new Error('recaptcha'))
      }
      s.onerror = () => { scriptPromise = null; reject(new Error('recaptcha')) }
      document.head.appendChild(s)
    })
  }
  return scriptPromise
}

let configPromise = null
function loadConfig() {
  if (!configPromise) {
    configPromise = api.get('/portal/auth/providers')
      .then((r) => r?.captcha || null)
      .catch(() => { configPromise = null; return null })
  }
  return configPromise
}

const captchaLoadError = () => new Error(translate('captcha.loadError'))

// Captcha config and a token getter.
//   config: undefined — loading, null — disabled, object — enabled
//   getToken(action): Promise<string>; returns '' when captcha is disabled
// The script loads as soon as the page opens: Google scores based on page
// behavior, so loading it only on click would lower the score.
export function useCaptcha() {
  const [config, setConfig] = useState(undefined)
  useEffect(() => {
    let alive = true
    loadConfig().then((c) => {
      if (!alive) return
      setConfig(c)
      if (c) loadRecaptcha(c.site_key).catch(() => { /* reported by getToken */ })
    })
    return () => { alive = false }
  }, [])

  const getToken = useCallback(async (action) => {
    const c = await loadConfig()
    if (!c) return ''
    let g
    try { g = await loadRecaptcha(c.site_key) } catch { throw captchaLoadError() }
    try {
      // A token is valid for 2 minutes and single-use — fetch a fresh one per submit.
      return await g.execute(c.site_key, { action })
    } catch {
      throw captchaLoadError()
    }
  }, [])

  return { config, getToken }
}

// Text required by Google (mandatory because the badge is hidden).
export function CaptchaNotice() {
  const t = useT()
  return (
    <p className="captcha-notice">
      {t('captcha.notice', {
        privacy: <a href="https://policies.google.com/privacy" target="_blank" rel="noopener noreferrer">{t('captcha.privacy')}</a>,
        terms: <a href="https://policies.google.com/terms" target="_blank" rel="noopener noreferrer">{t('captcha.terms')}</a>,
      })}
    </p>
  )
}
