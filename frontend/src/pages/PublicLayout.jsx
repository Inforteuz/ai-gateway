import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { useEffect } from 'react'
import { Link, NavLink, useLocation } from 'react-router-dom'
import { useAuth, useSite } from '../auth.js'
import { useT } from '../i18n/index.jsx'
import { Logo } from '../components/ui.jsx'
import TopBar from '../components/TopBar.jsx'
import RouteOutlet from '../components/RouteOutlet.jsx'

export default function PublicLayout() {
  useLocaleRefresh()
  const { user, freeMode } = useAuth()
  const site = useSite()
  const t = useT()
  const { pathname, hash } = useLocation()
  useEffect(() => {
    if (hash) document.getElementById(hash.slice(1))?.scrollIntoView({ behavior: 'smooth' })
    else window.scrollTo(0, 0)
  }, [pathname, hash])
  return (
    <div className="site">
      <TopBar />

      <main className="site-main"><RouteOutlet /></main>

      <footer className="site-foot">
        <div className="wrap">
          <div className="foot-grid">
            <div>
              <Link to="/" className="brand" style={{ marginBottom: 14 }}><Logo /> {tr(site.brand_name)} <small>AI API</small></Link>
              <p className="muted" style={{ maxWidth: '26rem', marginTop: 12 }}>
                {t('footer.tagline')}
              </p>
            </div>
            <div className="foot-col">
              <h4>{t('footer.product')}</h4>
              <ul>
                <li><Link to="/#imkoniyatlar">{t('nav.features')}</Link></li>
                <li><Link to="/#narxlar">{tr(freeMode ? t('nav.models') : t('nav.pricing'))}</Link></li>
                <li><NavLink to="/samples">{t('nav.samples')}</NavLink></li>
                <li><NavLink to="/compare">{t('nav.compare')}</NavLink></li>
                <li><NavLink to="/status">{t('nav.status')}</NavLink></li>
              </ul>
            </div>
            <div className="foot-col">
              <h4>{t('footer.getStarted')}</h4>
              <ul>
                <li><NavLink to="/docs">{t('nav.docs')}</NavLink></li>
                <li><Link to="/docs#curl">{t('footer.curl')}</Link></li>
                <li><Link to="/docs#python">Python SDK</Link></li>
                <li><Link to="/docs#stream">{tr("Streaming")}</Link></li>
              </ul>
            </div>
            <div className="foot-col">
              <h4>{t('footer.account')}</h4>
              <ul>
                {user
                  ? (<>
                      <li><Link to="/app">{t('footer.dashboard')}</Link></li>
                      <li><Link to="/app/keys">{t('footer.keys')}</Link></li>
                      <li><Link to="/app/usage">{t('footer.usage')}</Link></li>
                    </>)
                  : (<li><Link to="/login">{t('nav.login')}</Link></li>)}
              </ul>
            </div>
          </div>
          <div className="foot-bottom">
            <span>© {new Date().getFullYear()} {tr(site.brand_name)}</span>
            <span className="foot-legal">
              <NavLink to="/privacy">{t('footer.privacy')}</NavLink>
              <NavLink to="/terms">{t('footer.terms')}</NavLink>
            </span>
            <span>{t('footer.privacyNote')}</span>
          </div>
        </div>
      </footer>
    </div>
  )
}
