import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
// Password reset. ONE page handles both steps:
//   no `?token=` in the URL -> ask for the email, a link is sent via Telegram
//   `?token=` present       -> ask for the new password
// Two separate pages are unnecessary and would confuse users: they land here
// by tapping the link in Telegram.
import { useEffect, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { useSite } from '../auth.js'
import { LangSwitch, Logo, PasswordInput } from '../components/ui.jsx'
import { useT } from '../i18n/index.jsx'
import AuthArt from '../components/AuthArt.jsx'
import { CaptchaNotice, useCaptcha } from '../components/Captcha.jsx'
import { IconCheck } from '../components/icons.jsx'

// Same icons as on the sign-in page so the inputs look identical
const IconMail = (props) => (
  <svg {...props} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="3" y="5" width="18" height="14" rx="3" /><path d="m3.5 7 8.5 6 8.5-6" />
  </svg>
)
const IconLock = (props) => (
  <svg {...props} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <rect x="4.5" y="10" width="15" height="10.5" rx="3" /><path d="M8 10V7.5a4 4 0 1 1 8 0V10" />
  </svg>
)

const MIN_PASSWORD = 8

const POINTS = ['reset.point1', 'reset.point2', 'reset.point3']

export default function PasswordReset() {
  useLocaleRefresh()
  const site = useSite()
  const t = useT()
  const [params] = useSearchParams()
  const token = params.get(tr("token")) || ''
  const nav = useNavigate()

  // Check whether the link is still valid as soon as the page opens. Otherwise
  // the user would only see "link expired" after typing the new password.
  const [tokenState, setTokenState] = useState(token ? 'checking' : 'none')
  useEffect(() => {
    if (!token) return
    let alive = true
    api.get(`/portal/auth/password/reset-check?token=${encodeURIComponent(token)}`)
      .then((r) => { if (alive) setTokenState(r.valid ? 'valid' : 'expired') })
      .catch(() => { if (alive) setTokenState('expired') })
    return () => { alive = false }
  }, [token])

  return (
    <div className="auth">
      <aside className="auth-side">
        <div className="auth-side__top">
          <Link to="/" className="brand brand--rail">
            <Logo />
            <span><b>{tr(site.brand_name)}</b><small>AI API</small></span>
          </Link>
          <span className="auth-badge"><i aria-hidden="true" />{tr("v2.0 ·")} {t('reset.badge')}</span>
        </div>

        <div className="quote">
          <h2>{t('reset.quote')}<br /><em>{t('reset.quoteAccent')}</em></h2>
          <p>{t('reset.quoteText')}</p>
          <ul className="auth-points">
            {POINTS.map((k) => (
              <li key={k}><span className="auth-tick"><IconCheck /></span>{t(k)}</li>
            ))}
          </ul>
        </div>

        <AuthArt />
      </aside>

      <div className="auth-form">
        <div className="auth-lang"><LangSwitch /></div>
        {tokenState === 'none' && <EmailStep />}
        {tokenState === 'checking' && (
          <div className="auth-box">
            <p className="muted">{t('reset.checking')}</p>
          </div>
        )}
        {tokenState === 'expired' && <Expired />}
        {tokenState === 'valid' && <PasswordStep token={token} nav={nav} />}
      </div>
    </div>
  )
}

function EmailStep() {
  useLocaleRefresh()
  const t = useT()
  const [email, setEmail] = useState('')
  const [state, setState] = useState(null)   // null | 'sending' | 'sent'
  const [error, setError] = useState('')
  // reCAPTCHA v3: keeps bots from flooding strangers' Telegram with messages
  const captcha = useCaptcha()

  const submit = async (e) => {
    e.preventDefault()
    setError(''); setState('sending')
    try {
      const token = await captcha.getToken('password_reset')
      await api.post('/portal/auth/password/forgot', token ? { email, captcha_token: token } : { email })
      setState('sent')
    } catch (err) {
      setState(null)
      setError(err?.message || t('reset.sendFailed'))
    }
  }

  if (state === 'sent') {
    return (
      <div className="auth-box">
        <div className="auth-box__head">
          <h1>{t('reset.sentTitle')}</h1>
        </div>
        <p className="muted">{t('reset.sentText')}</p>
        <p className="muted small" style={{ marginTop: 16 }}>{t('reset.sentHelp')}</p>
        <Link className="btn" to="/login" style={{ marginTop: 20 }}>{t('reset.toLogin')}</Link>
      </div>
    )
  }

  return (
    <form className="auth-box" onSubmit={submit}>
      <div className="auth-box__head">
        <h1>{t('reset.title')}</h1>
        <p>{t('reset.lead', { via: <b>{t('reset.via')}</b> })}</p>
      </div>
      <label className="auth-field">
        <span className="lbl">{t('login.email')}</span>
        <span className="in-wrap">
          <IconMail className="lead" />
          <input className="input" type="email" value={email} required autoFocus
                 autoComplete="email" placeholder="you@example.com"
                 onChange={(e) => setEmail(e.target.value)} />
        </span>
      </label>
      {error && <p className="form-error">{tr(error)}</p>}
      <button className="btn btn--primary" type="submit"
              disabled={state === 'sending' || !email}
              style={{ width: '100%', marginTop: 16 }}>
        {tr(state === 'sending' ? t('reset.sending') : t('reset.send'))}
      </button>
      <p className="muted small" style={{ marginTop: 16, textAlign: 'center' }}>
        <Link to="/login">{t('reset.backToLogin')}</Link>
      </p>
      {captcha.config && <CaptchaNotice />}
    </form>
  )
}

function Expired() {
  useLocaleRefresh()
  const t = useT()
  return (
    <div className="auth-box">
      <div className="auth-box__head">
        <h1>{t('reset.expiredTitle')}</h1>
      </div>
      <p className="muted">{t('reset.expiredText')}</p>
      <Link className="btn btn--primary" to="/reset-password" style={{ marginTop: 20 }}>
        {t('reset.requestNew')}
      </Link>
    </div>
  )
}

function PasswordStep({ token, nav }) {
  useLocaleRefresh()
  const t = useT()
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const valid = password.length >= MIN_PASSWORD && password === confirm

  const submit = async (e) => {
    e.preventDefault()
    if (!valid) return
    setError(''); setSaving(true)
    try {
      await api.post('/portal/auth/password/reset', { token, password })
      // Changing the password ends all sessions, so the user has to sign in again.
      nav('/login?parol=yangilandi', { replace: true })
    } catch (err) {
      setSaving(false)
      setError(err?.message || t('reset.saveFailed'))
    }
  }

  return (
    <form className="auth-box" onSubmit={submit}>
      <div className="auth-box__head">
        <h1>{t('reset.newTitle')}</h1>
        <p>{t('reset.newLead')}</p>
      </div>
      <label className="auth-field">
        <span className="lbl">{t('reset.newPassword')}</span>
        <span className="in-wrap">
          <IconLock className="lead" />
          <PasswordInput required minLength={MIN_PASSWORD} value={password} autoFocus
                         autoComplete="new-password"
                         placeholder={t('login.passwordMin', { n: MIN_PASSWORD })}
                         onChange={(e) => setPassword(e.target.value)} />
        </span>
      </label>
      <label className="auth-field">
        <span className="lbl">{t('login.confirm')}</span>
        <span className="in-wrap">
          <IconLock className="lead" />
          <PasswordInput required minLength={MIN_PASSWORD} value={confirm}
                         autoComplete="new-password"
                         placeholder={t('login.confirmPh')}
                         onChange={(e) => setConfirm(e.target.value)} />
        </span>
      </label>
      {password && password.length < MIN_PASSWORD && (
        <p className="muted small">{t('reset.tooShort', { n: MIN_PASSWORD })}</p>
      )}
      {confirm && password !== confirm && <p className="form-error">{t('reset.mismatch')}</p>}
      {error && <p className="form-error">{tr(error)}</p>}
      <button className="btn btn--primary" type="submit" disabled={!valid || saving}
              style={{ width: '100%', marginTop: 16 }}>
        {tr(saving ? t('reset.saving') : t('reset.save'))}
      </button>
    </form>
  )
}
