import { Link, NavLink, useLocation } from 'react-router-dom'
import { useAuth, useSite } from '../auth.js'
import { useT } from '../i18n/index.jsx'
import { LangSwitch, Logo, ThemeToggle } from './ui.jsx'
import { IconArrowRight } from './icons.jsx'

/** Floating top bar from the mockup. Same on public pages and in the panel. */
export default function TopBar() {
  const { user, freeMode } = useAuth()
  const site = useSite()
  const t = useT()
  // Inside the panel "Go to panel" makes no sense — a back-to-site button is shown instead
  const inPanel = useLocation().pathname.startsWith('/app')
  return (
    <header className="site-top">
      <div className="site-top-inner">
        <Link to="/" className="brand"><Logo /> {site.brand_name} <small>AI API</small></Link>
        <nav className="site-nav" aria-label={t('nav.main')}>
          <Link to="/#imkoniyatlar">{t('nav.features')}</Link>
          <Link to="/#narxlar">{freeMode ? t('nav.models') : t('nav.pricing')}</Link>
          <NavLink to="/samples">{t('nav.samples')}</NavLink>
          <NavLink to="/compare">{t('nav.compare')}</NavLink>
          <NavLink to="/docs">{t('nav.docs')}</NavLink>
          <NavLink to="/status">{t('nav.status')}</NavLink>
        </nav>
        <div className="top-tools">
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
