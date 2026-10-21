import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useNavigate } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.js'
import { useT } from '../i18n/index.jsx'
import TopBar from '../components/TopBar.jsx'
import {
  IconActivity, IconArrowRight, IconBook, IconCard, IconChart, IconCoins,
  IconDoc, IconHome, IconKey, IconLayers, IconLogout, IconSettings, IconSparkles, IconUsers,
} from '../components/icons.jsx'

function RailLink({ to, end, children, badge, icon }) {
  return (
    <NavLink to={to} end={end} className={({ isActive }) => `rail-link ${isActive ? 'is-active' : ''}`}>
      {icon}
      <span>{children}</span>
      {badge > 0 && <span className="rail-badge">{badge}</span>}
    </NavLink>
  )
}

export default function Shell() {
  const { user, logout, freeMode } = useAuth()
  const nav = useNavigate()
  const t = useT()
  const [pending, setPending] = useState(0)
  const isAdmin = user.role === 'admin'

  useEffect(() => {
    if (!isAdmin) return
    let alive = true
    const load = () => api.get('/portal/admin/topups?status=pending').then((r) => alive && setPending(r.pending)).catch(() => {})
    load()
    const t = setInterval(load, 60000)
    return () => { alive = false; clearInterval(t) }
  }, [isAdmin])

  const who = user.name || user.email

  return (
    <div className="app">
      <TopBar />
      <div className="shell">
        <aside className="rail">
          <nav className="rail-nav" aria-label={t('shell.navLabel')}>
            <RailLink to="/app" end icon={<IconHome />}>{t('shell.overview')}</RailLink>
            <RailLink to="/app/keys" icon={<IconKey />}>{t('shell.keys')}</RailLink>
            <RailLink to="/app/catalog" icon={<IconLayers />}>{t('shell.catalog')}</RailLink>
            <RailLink to="/app/usage" icon={<IconChart />}>{t('shell.usage')}</RailLink>
            {!freeMode && <RailLink to="/app/billing" icon={<IconCard />}>{t('shell.billing')}</RailLink>}
            {!freeMode && <RailLink to="/app/referral" icon={<IconSparkles />}>{t('shell.referral')}</RailLink>}
            <RailLink to="/app/docs" icon={<IconDoc />}>{t('shell.docs')}</RailLink>
            {isAdmin && (
              <>
                <div className="rail-group">{t('shell.admin')}</div>
                <RailLink to="/app/admin" end icon={<IconChart />}>{t('shell.adminStats')}</RailLink>
                <RailLink to="/app/admin/monitoring" icon={<IconActivity />}>{t('shell.adminMonitoring')}</RailLink>
                <RailLink to="/app/admin/instances" icon={<IconLayers />}>{t('shell.adminInstances')}</RailLink>
                <RailLink to="/app/admin/users" icon={<IconUsers />}>{t('shell.adminUsers')}</RailLink>
                <RailLink to="/app/admin/topups" badge={pending} icon={<IconCoins />}>{t('shell.adminTopups')}</RailLink>
                <RailLink to="/app/admin/referrals" icon={<IconSparkles />}>{t('shell.adminReferrals')}</RailLink>
                <RailLink to="/app/admin/models" icon={<IconLayers />}>{t('shell.adminModels')}</RailLink>
                <RailLink to="/app/admin/settings" icon={<IconSettings />}>{t('shell.adminSettings')}</RailLink>
              </>
            )}
          </nav>
          {/* The admin menu is long — with the help card the rail would not fit
              on screen and get its own scrollbar. */}
          {!isAdmin && (
            <div className="rail-help">
              <b>{t('shell.helpTitle')}</b>
              <p>{t('shell.helpText')}</p>
              <Link to="/app/docs" className="btn btn--small"><IconBook /> {t('shell.helpOpen')} <IconArrowRight /></Link>
            </div>
          )}
          <div className="rail-foot">
            <NavLink to="/app/account" className="rail-user" title={user.email}>
              <span className="avatar" aria-hidden="true">{(who || '?').slice(0, 1).toUpperCase()}</span>
              <span className="who"><b>{who}</b><span>{user.email}</span></span>
            </NavLink>
            <button className="btn btn--small" onClick={async () => { await logout(); nav('/') }}>
              <IconLogout /> {t('shell.logout')}
            </button>
          </div>
        </aside>
        <main className="main"><Outlet /></main>
      </div>
    </div>
  )
}
