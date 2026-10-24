import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { useTheme } from '../theme.js'
import { LANGS, useLang, useT } from '../i18n/index.jsx'

// ---------- theme toggle ----------
export function ThemeToggle() {
  useLocaleRefresh()
  const { mode, cycle } = useTheme()
  const t = useT()
  const title = t('theme.title', { mode: t(`theme.${mode}`) })
  return (
    <button type="button" className="theme-toggle" onClick={cycle} title={tr(title)} aria-label={tr(title)}>
      {mode === 'auto' && (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="4" /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" opacity="0.5" /></svg>
      )}
      {mode === 'light' && (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="4.5" /><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" /></svg>
      )}
      {mode === 'dark' && (
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z" /></svg>
      )}
    </button>
  )
}

// ---------- language switcher ----------
export function LangSwitch() {
  useLocaleRefresh()
  const { lang, setLang } = useLang()
  const t = useT()
  return (
    <div className="lang-switch" role="group" aria-label={t('lang.label')}>
      {LANGS.map((l) => (
        <button key={l} type="button" className={l === lang ? 'is-on' : ''} aria-pressed={l === lang}
                title={t(`lang.name.${l}`)} onClick={() => setLang(l)}>
          {l.toUpperCase()}
        </button>
      ))}
    </div>
  )
}

// ---------- password field (with an eye toggle) ----------
export function PasswordInput({ value, onChange, ...props }) {
  useLocaleRefresh()
  const [show, setShow] = useState(false)
  const t = useT()
  return (
    <span className="pw-wrap">
      <input {...props} type={show ? 'text' : 'password'} className="input pw-input"
             value={value} onChange={onChange} />
      <button type="button" className="pw-eye" onClick={() => setShow((s) => !s)}
              tabIndex={-1} aria-label={show ? t('pw.hideLabel') : t('pw.showLabel')}
              title={show ? t('pw.hide') : t('pw.show')}>
        {show ? (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M9.88 9.88a3 3 0 004.24 4.24M10.73 5.08A10.43 10.43 0 0112 5c7 0 10 7 10 7a13.16 13.16 0 01-1.67 2.68M6.61 6.61A13.526 13.526 0 002 12s3 7 10 7a9.74 9.74 0 005.39-1.61" />
            <line x1="2" y1="2" x2="22" y2="22" />
          </svg>
        ) : (
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z" />
            <circle cx="12" cy="12" r="3" />
          </svg>
        )}
      </button>
    </span>
  )
}

// ---------- logo ----------
export function Logo({ size = 36, title }) {
  useLocaleRefresh()
  // Neutral mark: a spark inside a rounded square. Its color comes from the design
  // system's primary color (--primary), so it adapts to light and dark mode.
  // public/logo.svg and favicon.svg are static copies of this mark.
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 32 32"
      className="brand-logo"
      role={title ? 'img' : undefined}
      aria-label={title || undefined}
      aria-hidden={title ? undefined : true}
      style={{ display: 'block', flex: '0 0 auto' }}
    >
      <rect width="32" height="32" rx="8" fill="var(--primary, #00875a)" />
      <path d="M16 7c1 5 4 8 9 9-5 1-8 4-9 9-1-5-4-8-9-9 5-1 8-4 9-9z" fill="#ffffff" />
    </svg>
  )
}

// ---------- page heading (large display title from the mockup) ----------
/** `title` — text, or a [plain, accent] pair. */
export function PageHead({ eyebrow, title, lead, action, crumbs }) {
  useLocaleRefresh()
  const [head, accent] = Array.isArray(title) ? title : [title, null]
  return (
    <div className="page-head page-head--stack">
      {tr(crumbs)}
      {eyebrow && <span className="eyebrow-pill">{tr(eyebrow)}</span>}
      {/* The action sits on the title row at the right edge; the lead stays
          below at full width. */}
      <div className="page-head__row">
        <h1>{tr(head)}{accent && <em>{tr(accent)}</em>}</h1>
        {tr(action)}
      </div>
      {lead && <p>{tr(lead)}</p>}
    </div>
  )
}

// ---------- stat card ----------
export function StatCard({ icon, label, value, unit, trend, trendDir, note, tone }) {
  useLocaleRefresh()
  return (
    <div className="metric">
      <span className="label"><span className={`ico-chip ico-chip--sm ${tone ? `ico-chip--${tone}` : ''}`}>{tr(icon)}</span>{tr(label)}</span>
      <b>{tr(value)}{unit && <small>{tr(unit)}</small>}</b>
      {(trend || note) && (
        <span className="trend">
          {trend && <span className={`trend-pill ${trendDir === 'down' ? 'trend-pill--down' : trendDir === 'up' ? 'trend-pill--up' : ''}`}>{tr(trend)}</span>}
          {tr(note)}
        </span>
      )}
    </div>
  )
}

// ---------- icon-only copy button ----------
export function CopyIcon({ text, title }) {
  useLocaleRefresh()
  const [done, setDone] = useState(false)
  const t = useT()
  const label = done ? t('common.copied') : (title || t('common.copy'))
  const copy = async () => {
    try { await navigator.clipboard.writeText(text) } catch {
      const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select()
      try { document.execCommand('copy') } catch { /* ignore */ }
      ta.remove()
    }
    setDone(true); setTimeout(() => setDone(false), 1600)
  }
  return (
    <button type="button" className="icon-btn" onClick={copy} title={tr(label)} aria-label={tr(label)}>
      {done
        ? <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m5 12.5 4.5 4.5L19 7" /></svg>
        : <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="12" height="12" rx="2.5" /><path d="M15 6.5V5.5A2.5 2.5 0 0 0 12.5 3H5.5A2.5 2.5 0 0 0 3 5.5v7A2.5 2.5 0 0 0 5.5 15h1" /></svg>}
    </button>
  )
}

// ---------- data loading ----------
/** Loads data. With `refreshMs` it keeps refreshing itself.
 *
 * Background refreshes are SILENT — `loading` is not raised and transient
 * errors are not shown. Otherwise tables would "jump" every few seconds, or
 * a brief network blip would replace good data with an error.
 *
 * While the page is hidden (another tab, minimized window) no requests are
 * sent; on return it refreshes once immediately.
 */
export function useLoad(fn, deps = [], { refreshMs = 0 } = {}) {
  const [state, setState] = useState({ data: null, error: null, loading: true })
  const seq = useRef(0)
  const run = useCallback((silent = false) => {
    const id = ++seq.current
    if (!silent) setState((s) => ({ ...s, loading: true, error: null }))
    return fn().then(
      (data) => id === seq.current && setState({ data, error: null, loading: false }),
      (error) => id === seq.current && setState((s) => ({
        data: s.data, error: silent ? s.error : error, loading: false,
      })),
    )
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  useEffect(() => { run() }, [run])

  useEffect(() => {
    if (!refreshMs) return undefined
    const tick = () => { if (document.visibilityState === 'visible') run(true) }
    const t = setInterval(tick, refreshMs)
    document.addEventListener('visibilitychange', tick)
    return () => { clearInterval(t); document.removeEventListener('visibilitychange', tick) }
  }, [run, refreshMs])

  // `reload` may be passed straight to a button (`onClick={reload}`), in which
  // case React passes the event object — arguments are dropped on purpose so
  // it isn't read as `silent`.
  const reload = useCallback(() => run(false), [run])
  return { ...state, reload }
}

// ---------- toasts ----------
const ToastCtx = createContext(() => {})
export const useToast = () => useContext(ToastCtx)

export function ToastProvider({ children }) {
  useLocaleRefresh()
  const [items, setItems] = useState([])
  const push = useCallback((message, kind = 'ok') => {
    const id = Math.random()
    setItems((x) => [...x, { id, message, kind }])
    setTimeout(() => setItems((x) => x.filter((i) => i.id !== id)), kind === 'error' ? 6000 : 3200)
  }, [])
  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div className="toast-area" role="status" aria-live="polite">
        {items.map((t) => <div key={t.id} className={`toast ${t.kind === 'error' ? 'toast--error' : ''}`}>{tr(t.message)}</div>)}
      </div>
    </ToastCtx.Provider>
  )
}

// ---------- modal ----------
export function Modal({ title, onClose, children, wide, actions }) {
  useLocaleRefresh()
  useEffect(() => {
    const onKey = (e) => e.key === 'Escape' && onClose?.()
    document.addEventListener('keydown', onKey)
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.removeEventListener('keydown', onKey); document.body.style.overflow = prev }
  }, [onClose])
  return (
    <div className="overlay" onMouseDown={(e) => e.target === e.currentTarget && onClose?.()}>
      <div className={`modal ${wide ? 'modal--wide' : ''}`} role="dialog" aria-modal="true" aria-label={tr(title)}>
        <h2>{tr(title)}</h2>
        {children}
        {actions && <div className="modal-actions">{tr(actions)}</div>}
      </div>
    </div>
  )
}

// ---------- copy button ----------
export function CopyButton({ text, label, small = true }) {
  useLocaleRefresh()
  const [done, setDone] = useState(false)
  const t = useT()
  const copy = async () => {
    try { await navigator.clipboard.writeText(text) } catch {
      const ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta); ta.select()
      try { document.execCommand('copy') } catch { /* ignore */ }
      ta.remove()
    }
    setDone(true); setTimeout(() => setDone(false), 1600)
  }
  return <button type="button" className={`btn ${small ? 'btn--small' : ''}`} onClick={copy}>{tr(done ? t('common.copied') : (label || t('common.copy')))}</button>
}

export function CodeBlock({ code }) {
  useLocaleRefresh()
  return (
    <div className="code">
      <span className="copy"><CopyButton text={code} /></span>
      <pre><code>{code}</code></pre>
    </div>
  )
}

export function CodeTabs({ tabs }) {
  useLocaleRefresh()
  const [i, setI] = useState(0)
  return (
    <div>
      <div className="code-tabs" role="tablist">
        {tabs.map((t, k) => <button key={t.name} role="tab" aria-selected={k === i} className={`code-tab ${k === i ? 'is-on' : ''}`} onClick={() => setI(k)}>{tr(t.name)}</button>)}
      </div>
      <CodeBlock code={tabs[i].code} />
    </div>
  )
}

// ---------- empty state, error, pagination ----------
export function Empty({ title, children }) {
  useLocaleRefresh()
  return <div className="empty"><b>{tr(title)}</b>{children}</div>
}

export function ErrorNote({ error, retry }) {
  useLocaleRefresh()
  const t = useT()
  if (!error) return null
  return (
    <div className="alert alert--error" role="alert">
      {tr(error.message)} {retry && <button className="btn btn--link" onClick={retry}>{t('common.retry')}</button>}
    </div>
  )
}

export function Loading({ rows = 3 }) {
  useLocaleRefresh()
  return <div style={{ display: 'grid', gap: 14 }} aria-busy="true">{Array.from({ length: rows }).map((_, i) => <div key={i} className="skeleton" style={{ width: `${88 - i * 14}%`, height: 18 }} />)}</div>
}

export function Pager({ total, limit, offset, onChange }) {
  useLocaleRefresh()
  const t = useT()
  if (total <= limit) return null
  const from = offset + 1, to = Math.min(offset + limit, total)
  return (
    <div className="pager">
      <span>{tr(from)}-{tr(to)} / {tr(total)}</span>
      <div>
        <button className="btn btn--small" disabled={offset === 0} onClick={() => onChange(Math.max(0, offset - limit))}>{t('common.prev')}</button>
        <button className="btn btn--small" disabled={to >= total} onClick={() => onChange(offset + limit)}>{t('common.next')}</button>
      </div>
    </div>
  )
}

export function Status({ online }) {
  useLocaleRefresh()
  const t = useT()
  return (
    <span className="status-line">
      <span className={`dot ${online ? 'dot--on' : 'dot--off'}`} />
      {tr(online ? t('status.online') : t('status.offline'))}
    </span>
  )
}
