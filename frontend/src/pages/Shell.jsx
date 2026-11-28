import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { useEffect, useRef, useState } from 'react'
import { Link, NavLink, useLocation, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth, useSite } from '../auth.js'
import { useT } from '../i18n/index.jsx'
import { useTheme } from '../theme.js'
import { fmtSom } from '../format.js'
import RouteOutlet from '../components/RouteOutlet.jsx'
import UserAvatar from '../components/Avatar.jsx'
import { LangSwitch, Logo, useLoad } from '../components/ui.jsx'
import {
  IconActivity, IconCard, IconChart, IconCoins, IconDoc, IconHome, IconKey,
  IconLayers, IconLogout, IconSettings, IconSparkles, IconUser, IconUsers, IconWallet, IconZap,
} from '../components/icons.jsx'

const RAIL_KEY = 'sf-rail'

function SideLink({ to, end, children, badge, icon, collapsed }) {
  useLocaleRefresh()
  return (
    <NavLink to={to} end={end} title={collapsed ? String(children) : undefined}
      className={({ isActive }) => `side-link ${isActive ? 'is-active' : ''}`}>
      {tr(icon)}
      <span className="side-link__text">{children}</span>
      {badge > 0 && <span className="side-badge">{tr(badge)}</span>}
    </NavLink>
  )
}

function ThemeIcon({ mode }) {
  if (mode === 'dark') return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z" /></svg>
  return (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41" opacity={mode === 'auto' ? 0.5 : 1} />
    </svg>
  )
}

/** Account menu in the top bar: profile, back to the site, sign out. */
function UserMenu({ user }) {
  useLocaleRefresh()
  const t = useT()
  const nav = useNavigate()
  const { logout } = useAuth()
  const [open, setOpen] = useState(false)
  const ref = useRef(null)
  const { pathname } = useLocation()
  useEffect(() => { setOpen(false) }, [pathname])
  useEffect(() => {
    if (!open) return undefined
    const onKey = (e) => { if (e.key === 'Escape') setOpen(false) }
    const onClick = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false) }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onClick)
    return () => { document.removeEventListener('keydown', onKey); document.removeEventListener('mousedown', onClick) }
  }, [open])
  const who = user.name || user.email
  return (
    <div className="umenu" ref={ref}>
      <button type="button" className="umenu-btn" aria-haspopup="menu" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        <UserAvatar user={user} />
        <span className="umenu-who"><b>{tr(who)}</b><small>{tr(user.role === 'admin' ? t('shell.admin') : user.email)}</small></span>
        <svg className="umenu-caret" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="m6 9 6 6 6-6" /></svg>
      </button>
      {open && (
        <div className="umenu-pop" role="menu">
          <div className="umenu-head">
            <UserAvatar user={user} className="avatar avatar--lg" />
            <span className="umenu-head__t"><b>{tr(who)}</b><span>{tr(user.email)}</span></span>
          </div>
          <Link role="menuitem" to="/app/account"><IconUser /> {tr('Profil')}</Link>
          <Link role="menuitem" to="/"><IconHome /> {t('nav.home')}</Link>
          <button role="menuitem" type="button" onClick={async () => { await logout(); nav('/') }}>
            <IconLogout /> {t('shell.logout')}
          </button>
        </div>
      )}
    </div>
  )
}

export default function Shell() {
  useLocaleRefresh()
  const { user, freeMode } = useAuth()
  const site = useSite()
  const t = useT()
  const { mode, cycle } = useTheme()
  const [pending, setPending] = useState(0)
  const [drawer, setDrawer] = useState(false)
  const [collapsed, setCollapsed] = useState(() => {
    try { return localStorage.getItem(RAIL_KEY) === 'mini' } catch { return false }
  })
  const isAdmin = user.role === 'admin'
  const { pathname } = useLocation()
  // Balance in the top bar; the summary is cheap and pages refresh it on their own too.
  const sum = useLoad(() => (freeMode ? Promise.resolve(null) : api.get('/portal/account/summary')), [freeMode], { refreshMs: 30_000 })

  useEffect(() => { setDrawer(false) }, [pathname])
  useEffect(() => {
    if (!drawer) return undefined
    const onKey = (e) => { if (e.key === 'Escape') setDrawer(false) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [drawer])
  useEffect(() => {
    try { localStorage.setItem(RAIL_KEY, collapsed ? 'mini' : 'full') } catch { /* private window */ }
  }, [collapsed])

  useEffect(() => {
    if (!isAdmin) return
    let alive = true
    const load = () => api.get('/portal/admin/topups?status=pending').then((r) => alive && setPending(r.pending)).catch(() => {})
    load()
    const id = setInterval(load, 60000)
    return () => { alive = false; clearInterval(id) }
  }, [isAdmin])

  const c = collapsed
  const s = sum.data
  const themeLabel = t('theme.title', { mode: t(`theme.${mode}`) })

  return (
    <div className={`console ${c ? 'is-mini' : ''} ${drawer ? 'is-drawer' : ''}`}>
      <aside className="side" id="console-side" aria-label={t('shell.navLabel')}>
        <div className="side-brand">
          <Link to="/app" className="side-logo" title={tr(site.brand_name)}>
            <Logo size={34} />
            <span className="side-link__text">{tr(site.brand_name)}</span>
          </Link>
          <button type="button" className="side-close" aria-label={tr('Menyuni yopish')} onClick={() => setDrawer(false)}>
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
          </button>
        </div>
        <nav className="side-nav">
          <div className="side-group">
            <SideLink collapsed={c} to="/app" end icon={<IconHome />}>{t('shell.overview')}</SideLink>
            <SideLink collapsed={c} to="/app/keys" icon={<IconKey />}>{t('shell.keys')}</SideLink>
            <SideLink collapsed={c} to="/app/playground" icon={<IconZap />}>{t('shell.playground')}</SideLink>
            <SideLink collapsed={c} to="/app/catalog" icon={<IconLayers />}>{t('shell.catalog')}</SideLink>
          </div>
          <div className="side-group">
            <span className="side-group__t">{tr('Hisob va sarf')}</span>
            <SideLink collapsed={c} to="/app/usage" icon={<IconChart />}>{t('shell.usage')}</SideLink>
            {!freeMode && <SideLink collapsed={c} to="/app/billing" icon={<IconCard />}>{t('shell.billing')}</SideLink>}
            {!freeMode && <SideLink collapsed={c} to="/app/referral" icon={<IconSparkles />}>{t('shell.referral')}</SideLink>}
            <SideLink collapsed={c} to="/app/docs" icon={<IconDoc />}>{t('shell.docs')}</SideLink>
          </div>
          {isAdmin && (
            <div className="side-group">
              <span className="side-group__t">{t('shell.admin')}</span>
              <SideLink collapsed={c} to="/app/admin" end icon={<IconChart />}>{t('shell.adminStats')}</SideLink>
              <SideLink collapsed={c} to="/app/admin/monitoring" icon={<IconActivity />}>{t('shell.adminMonitoring')}</SideLink>
              <SideLink collapsed={c} to="/app/admin/usage" icon={<IconChart />}>{tr("So‘rovlar va tokenlar")}</SideLink>
              <SideLink collapsed={c} to="/app/admin/instances" icon={<IconLayers />}>{t('shell.adminInstances')}</SideLink>
              <SideLink collapsed={c} to="/app/admin/users" icon={<IconUsers />}>{t('shell.adminUsers')}</SideLink>
              <SideLink collapsed={c} to="/app/admin/topups" badge={pending} icon={<IconCoins />}>{t('shell.adminTopups')}</SideLink>
              <SideLink collapsed={c} to="/app/admin/referrals" icon={<IconSparkles />}>{t('shell.adminReferrals')}</SideLink>
              <SideLink collapsed={c} to="/app/admin/models" icon={<IconLayers />}>{t('shell.adminModels')}</SideLink>
              <SideLink collapsed={c} to="/app/admin/settings" icon={<IconSettings />}>{t('shell.adminSettings')}</SideLink>
            </div>
          )}
        </nav>
        <div className="side-foot">
          <button type="button" className="side-link" onClick={cycle} title={tr(themeLabel)} aria-label={tr(themeLabel)}>
            <ThemeIcon mode={mode} />
            <span className="side-link__text">{t(`theme.${mode}`)}</span>
          </button>
          <button type="button" className="side-link side-collapse" onClick={() => setCollapsed((v) => !v)}
            aria-pressed={c} title={tr(c ? 'Menyuni yoyish' : 'Menyuni yig‘ish')} aria-label={tr(c ? 'Menyuni yoyish' : 'Menyuni yig‘ish')}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              {c ? <path d="m7 6 6 6-6 6M13 6l6 6-6 6" /> : <path d="m17 6-6 6 6 6M11 6l-6 6 6 6" />}
            </svg>
            <span className="side-link__text">{tr('Menyuni yig‘ish')}</span>
          </button>
        </div>
      </aside>
      <div className="side-scrim" aria-hidden="true" onClick={() => setDrawer(false)} />

      <div className="console-body">
        <header className="ctop">
          <button type="button" className="ctop-burger" aria-controls="console-side" aria-expanded={drawer}
            aria-label={tr('Menyu')} onClick={() => setDrawer(true)}>
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true"><path d="M4 7h16M4 12h16M4 17h16" /></svg>
          </button>
          <Link to="/app" className="ctop-brand" aria-label={tr(site.brand_name)}><Logo size={30} /></Link>
          <div className="ctop-tools">
            {!freeMode && s && (
              <Link to="/app/billing" className="ctop-balance" title={tr('Hisobdagi mablag‘')}>
                <IconWallet /> <span>{fmtSom(s.balance)}</span>
              </Link>
            )}
            <LangSwitch />
            <UserMenu user={user} />
          </div>
        </header>
        <main className="console-main"><RouteOutlet /></main>
      </div>
    </div>
  )
}
