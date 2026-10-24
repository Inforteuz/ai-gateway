import { translateText as tr } from '../i18n/index.jsx'
// Sign-in buttons for Google and Telegram.
//
// Buttons are only rendered when the server reports them as configured: with
// no keys `/portal/auth/providers` returns nothing and this block is hidden.
// Both flows redirect the browser to the server, so no tokens or signatures
// are handled here.
import { useEffect, useRef, useState } from 'react'
import { api } from '../api.js'
import { useLang, useT } from '../i18n/index.jsx'

// Error codes returned by the server. The text lives here so the `oauth_error`
// value in the URL can't inject its own message into the page.
export const OAUTH_ERROR_CODES = [
  'google_cancelled', 'google_state', 'google_unreachable', 'google_rejected', 'google_bad_token',
  'google_expired', 'google_no_email', 'google_not_configured', 'telegram_bad_data',
  'telegram_bad_signature', 'telegram_expired', 'telegram_not_configured', 'registration_closed',
  'too_many_accounts', 'already_linked', 'account_suspended',
]

export const oauthErrorText = (t, code) => {
  if (!code) return ''
  return OAUTH_ERROR_CODES.includes(code) ? t(`oauth.${code}`) : t('oauth.generic')
}

const GoogleMark = () => (
  <svg width="18" height="18" viewBox="0 0 48 48" aria-hidden="true">
    <path fill="#4285F4" d="M45.1 24.5c0-1.6-.1-3.1-.4-4.5H24v8.5h11.8c-.5 2.7-2.1 5-4.4 6.6v5.5h7.1c4.2-3.8 6.6-9.5 6.6-16.1z" />
    <path fill="#34A853" d="M24 46c6 0 11-2 14.5-5.4l-7.1-5.5c-2 1.3-4.5 2.1-7.4 2.1-5.7 0-10.6-3.8-12.3-9H4.3v5.7C7.9 41.1 15.4 46 24 46z" />
    <path fill="#FBBC05" d="M11.7 28.2c-.4-1.3-.7-2.7-.7-4.2s.3-2.9.7-4.2v-5.7H4.3C2.8 17.1 2 20.4 2 24s.8 6.9 2.3 9.9l7.4-5.7z" />
    <path fill="#EA4335" d="M24 10.4c3.2 0 6.1 1.1 8.4 3.3l6.3-6.3C34.9 3.9 29.9 2 24 2 15.4 2 7.9 6.9 4.3 14.1l7.4 5.7c1.7-5.2 6.6-9.4 12.3-9.4z" />
  </svg>
)

function TelegramButton({ bot }) {
  const box = useRef(null)
  const { lang } = useLang()

  useEffect(() => {
    const host = box.current
    if (!host) return
    // Telegram's own script draws the button inside an <iframe>. Clear first so
    // a re-render doesn't add a second button.
    host.innerHTML = ''
    const script = document.createElement('script')
    script.src = 'https://telegram.org/js/telegram-widget.js?22'
    script.async = true
    script.setAttribute('data-telegram-login', bot)
    // Without this the widget follows the browser language rather than the
    // site's. If Telegram doesn't support Uzbek it falls back to English.
    script.setAttribute('data-lang', lang)
    script.setAttribute('data-size', 'large')
    script.setAttribute('data-radius', '10')
    script.setAttribute('data-userpic', 'false')
    // After confirmation Telegram sends the browser back to this URL; the
    // signature is verified on the server with the bot token.
    script.setAttribute('data-auth-url', `${window.location.origin}/portal/auth/telegram/callback`)
    host.appendChild(script)
    return () => { host.innerHTML = '' }
  }, [bot, lang])

  return <div className="social-widget" ref={box} />
}

export default function SocialLogin({ mode = 'login' }) {
  const [providers, setProviders] = useState(null)
  const t = useT()

  useEffect(() => {
    let alive = true
    api.get('/portal/auth/providers')
      .then((r) => { if (alive) setProviders(r) })
      .catch(() => { if (alive) setProviders({ google: false, telegram: '' }) })
    return () => { alive = false }
  }, [])

  if (!providers?.google && !providers?.telegram) return null

  return (
    <div className="social">
      <div className="social-divider"><span>{t('social.or')}</span></div>
      {providers.google && (
        <a className="btn btn--large social-btn" href="/portal/auth/google/start">
          <GoogleMark /> {t('social.google')}
        </a>
      )}
      {providers.telegram && (
        <>
          {/* Telegram draws the button text inside its widget, so the mode is
              stated by the label above it. */}
          <span className="social-label">
            {tr(mode === 'register' ? t('social.telegramRegister') : t('social.telegramLogin'))}
          </span>
          <TelegramButton bot={providers.telegram} />
        </>
      )}
      <p className="dim small" style={{ textAlign: 'center', margin: 0 }}>
        {t('social.linkNote')}
      </p>
    </div>
  )
}
