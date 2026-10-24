import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { useState } from 'react'
import { Link, Navigate, useLocation, useNavigate, useSearchParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth, useSite } from '../auth.js'
import { LangSwitch, Logo, PasswordInput } from '../components/ui.jsx'
import { useT } from '../i18n/index.jsx'
import AuthArt from '../components/AuthArt.jsx'
import SocialLogin, { oauthErrorText } from '../components/SocialLogin.jsx'
import { CaptchaNotice, useCaptcha } from '../components/Captcha.jsx'
import { IconArrowRight, IconCheck, IconUser } from '../components/icons.jsx'

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

const POINTS = ['login.point1', 'login.point2', 'login.point3']

export default function Login() {
  useLocaleRefresh()
  const { user, loading, registrationOpen, refresh } = useAuth()
  const site = useSite()
  const t = useT()
  const nav = useNavigate()
  const loc = useLocation()
  const [params] = useSearchParams()
  // The landing page's "Sign up free" button opens this mode directly.
  const [mode, setMode] = useState(params.get('mode') === 'register' ? 'register' : 'login')
  const [form, setForm] = useState({ email: '', password: '', name: '', again: '' })
  const [remember, setRemember] = useState(true)
  const [agree, setAgree] = useState(false)
  // If the Google/Telegram flow fails, the server redirects back here with a
  // code (the message text lives in the frontend, see SocialLogin.jsx).
  const [oauthCode, setOauthCode] = useState(() => params.get('oauth_error'))
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const register = mode === 'register'
  // reCAPTCHA v3 (invisible): a token is fetched on every submit. For sign-in
  // the server decides whether to check it (always, or after N failures).
  const captcha = useCaptcha()

  if (!loading && user) return <Navigate to={loc.state?.from?.startsWith('/app') ? loc.state.from : '/app'} replace />

  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))
  const toggleMode = () => {
    setMode(register ? 'login' : 'register')
    setError(''); setOauthCode(null)
  }

  const submit = async (e) => {
    e.preventDefault()
    if (register && form.password !== form.again) { setError(t('login.mismatch')); setOauthCode(null); return }
    setBusy(true); setError(''); setOauthCode(null)
    try {
      const token = await captcha.getToken(register ? 'register' : 'login')
      const extra = token ? { captcha_token: token } : {}
      const body = register
        ? { email: form.email, name: form.name, password: form.password, ...extra }
        // `remember` is not read by the server yet: session lifetime comes from
        // settings. It is sent ahead of time so the frontend needs no change
        // once the backend supports it.
        : { email: form.email, password: form.password, remember, ...extra }
      await api.post(register ? '/portal/auth/register' : '/portal/auth/login', body)
      await refresh()
      nav(loc.state?.from?.startsWith('/app') ? loc.state.from : '/app', { replace: true })
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="auth">
      <aside className="auth-side">
        <div className="auth-side__top">
          <Link to="/" className="brand brand--rail">
            <Logo />
            <span><b>{tr(site.brand_name)}</b><small>AI API</small></span>
          </Link>
          <span className="auth-badge"><i aria-hidden="true" />{tr("v2.0 ·")} {t('login.badge')}</span>
        </div>

        <div className="quote">
          <h2>{t('login.quote')}<br /><em>{t('login.quoteAccent')}</em></h2>
          <p>{t('login.quoteText')}</p>
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
        <form className="auth-box" onSubmit={submit}>
          <div className="auth-box__head">
            <h1>{tr(register ? t('login.register') : t('login.signIn'))}</h1>
            <p>
              {tr(register
                ? t('login.registerLead')
                : t('login.signInLead', { brand: site.brand_name }))}
            </p>
          </div>

          {(error || oauthCode) && <div className="alert alert--error" role="alert">{tr(error || oauthErrorText(t, oauthCode))}</div>}

          {register && (
            <label className="auth-field">
              <span className="lbl">{t('login.name')}</span>
              <span className="in-wrap">
                <IconUser className="lead" />
                <input className="input" value={form.name} onChange={set('name')}
                       autoComplete="name" maxLength={80} placeholder={t('login.namePh')} />
              </span>
            </label>
          )}

          <label className="auth-field">
            <span className="lbl">{t('login.email')}</span>
            <span className="in-wrap">
              <IconMail className="lead" />
              <input className="input" type="email" required value={form.email} onChange={set('email')}
                     autoComplete="email" autoFocus placeholder="you@example.com" />
            </span>
          </label>

          <label className="auth-field">
            <span className="lbl">{t('login.password')}</span>
            <span className="in-wrap">
              <IconLock className="lead" />
              <PasswordInput required minLength={register ? 8 : 1}
                             value={form.password} onChange={set('password')}
                             autoComplete={register ? 'new-password' : 'current-password'}
                             placeholder={register ? t('login.passwordMin', { n: 8 }) : t('login.passwordPh')} />
            </span>
          </label>

          {register && (
            <label className="auth-field">
              <span className="lbl">{t('login.confirm')}</span>
              <span className="in-wrap">
                <IconLock className="lead" />
                <PasswordInput required minLength={8} value={form.again} onChange={set('again')}
                               autoComplete="new-password" placeholder={t('login.confirmPh')} />
              </span>
            </label>
          )}

          {register ? (
            <label className="check auth-check">
              <input type="checkbox" checked={agree} onChange={(e) => setAgree(e.target.checked)} required />
              <span>
                {t('login.agree', {
                  terms: <Link to="/terms">{t('login.terms')}</Link>,
                  privacy: <Link to="/privacy">{t('login.privacy')}</Link>,
                })}
              </span>
            </label>
          ) : (
            <div className="auth-row">
              <label className="check">
                <input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
                <span>{t('login.remember')}</span>
              </label>
              <Link to="/reset-password" className="auth-link">{t('login.forgot')}</Link>
            </div>
          )}

          <button className="btn btn--primary btn--large auth-submit" disabled={busy}>
            {tr(busy ? t('login.wait') : <>{tr(register ? t('login.submitRegister') : t('login.submitSignIn'))} <IconArrowRight /></>)}
          </button>

          <SocialLogin mode={mode} />
          {captcha.config && <CaptchaNotice />}

          {registrationOpen && (
            <p className="auth-foot">
              {tr(register ? t('login.haveAccount') : t('login.noAccount'))}{' '}
              <button type="button" className="auth-link" onClick={toggleMode}>
                {tr(register ? t('login.submitSignIn') : t('login.registerCta'))}
              </button>
            </p>
          )}
          {!registrationOpen && (
            <p className="auth-foot dim">
              {t('login.closed')}
            </p>
          )}
        </form>
      </div>
    </div>
  )
}
