import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { useEffect, useRef, useState } from 'react'
import { Link, NavLink, useLocation } from 'react-router-dom'
import { useAuth, useSite } from '../auth.js'
import { useT } from '../i18n/index.jsx'
import { LangSwitch, Logo, ThemeToggle } from './ui.jsx'
import { IconArrowRight } from './icons.jsx'

/** Floating top bar from the mockup. Same on public pages and in the panel. */
export default function TopBar() {
  useLocaleRefresh()
  const { user, freeMode } = useAuth()
  const site = useSite()
  const t = useT()
  // Inside the panel "Go to panel" makes no sense — a back-to-site button is shown instead
  const location = useLocation()
  const inPanel = location.pathname.startsWith('/app')
  // Below 1180px the six links do not fit next to the tools (Russian labels are the
  // longest), so they move into a menu that opens under the bar.
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  useEffect(() => { setOpen(false) }, [location.pathname, location.hash])
  useEffect(() => {
    if (!open) return undefined
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    const onClick = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onClick)
    return () => { document.removeEventListener('keydown', onKey); document.removeEventListener('mousedown', onClick) }
  }, [open])
  return (
    <header className={`site-top ${open ? 'is-open' : ''}`} ref={ref}>
      <div className="site-top-inner">
        <Link to="/" className="brand"><Logo /> {tr(site.brand_name)} <small>AI API</small></Link>
        <nav id="site-nav" className="site-nav" aria-label={t('nav.main')}>
          <Link to="/#imkoniyatlar">{t('nav.features')}</Link>
          <Link to="/#narxlar">{tr(freeMode ? t('nav.models') : t('nav.pricing'))}</Link>
          <NavLink to="/samples">{t('nav.samples')}</NavLink>
          <NavLink to="/compare">{t('nav.compare')}</NavLink>
          <NavLink to="/docs">{t('nav.docs')}</NavLink>
          <NavLink to="/status">{t('nav.status')}</NavLink>
        </nav>
        <div className="top-tools">
          <button type="button" className="nav-toggle" aria-expanded={open} aria-controls="site-nav"
            aria-label={tr('Menyu')} onClick={() => setOpen((v) => !v)}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
              {open ? <path d="M6 6l12 12M18 6L6 18" /> : <path d="M4 7h16M4 12h16M4 17h16" />}
            </svg>
          </button>
          <LangSwitch />
          <ThemeToggle />
          {user
            ? (inPanel
                ? <Link to="/" className="btn">{t('nav.home')}</Link>
                : <Link to="/app" className="btn btn--primary">{t('nav.toPanel')} <IconArrowRight /></Link>)
            : <Link to="/login" className="btn btn--primary">{t('nav.login')} <IconArrowRight /></Link>}
        </div>
      </div>
    </header>
  )
}
