import { useCallback, useEffect, useState } from 'react'
import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import { api } from './api.js'
import { AuthCtx, useAuth } from './auth.js'
import { forgetReferral } from './referral.js'
import { normalizeSite } from './site.js'
import { LangProvider } from './i18n/index.jsx'
import { ToastProvider } from './components/ui.jsx'
import Shell from './pages/Shell.jsx'
import Landing from './pages/Landing.jsx'
import Login from './pages/Login.jsx'
import Docs from './pages/Docs.jsx'
import PublicLayout from './pages/PublicLayout.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Keys from './pages/Keys.jsx'
import Usage from './pages/Usage.jsx'
import Billing from './pages/Billing.jsx'
import Account from './pages/Account.jsx'
import Catalog from './pages/Catalog.jsx'
import ModelDetail from './pages/ModelDetail.jsx'
import AdminOverview from './pages/admin/Overview.jsx'
import AdminUsers from './pages/admin/Users.jsx'
import AdminTopups from './pages/admin/Topups.jsx'
import AdminModels from './pages/admin/Models.jsx'
import AdminSettings from './pages/admin/Settings.jsx'
import AdminMonitoring from './pages/admin/Monitoring.jsx'
import AdminReferrals from './pages/admin/Referrals.jsx'
import AdminInstances from './pages/admin/Instances.jsx'
import AdminVnc from './pages/admin/Vnc.jsx'
import Referral from './pages/Referral.jsx'
import Status from './pages/Status.jsx'
import Samples from './pages/Samples.jsx'
import Compare from './pages/Compare.jsx'
import PasswordReset from './pages/PasswordReset.jsx'
import Privacy from './pages/Privacy.jsx'
import Terms from './pages/Terms.jsx'

function RequireAuth() {
  const { user, loading } = useAuth()
  const loc = useLocation()
  if (loading) return null
  if (!user) return <Navigate to="/login" replace state={{ from: loc.pathname }} />
  return <Outlet />
}

function RequireAdmin() {
  const { user } = useAuth()
  if (user?.role !== 'admin') return <Navigate to="/app" replace />
  return <Outlet />
}

export default function App() {
  const [auth, setAuth] = useState({ loading: true, user: null, registrationOpen: false, freeMode: false, site: normalizeSite(null) })

  const refresh = useCallback(async () => {
    try {
      const r = await api.get('/portal/auth/me')
      setAuth({ loading: false, user: r.user, registrationOpen: r.registration_open, freeMode: !!r.free_mode, site: normalizeSite(r.site) })
    } catch {
      setAuth((a) => ({ ...a, loading: false }))
    }
  }, [])
  useEffect(() => { refresh() }, [refresh])
  // The referral code only matters for new accounts — don't keep it for signed-in users.
  useEffect(() => { if (auth.user) forgetReferral() }, [auth.user])
  // Browser title from the platform name (pages that set their own title win)
  useEffect(() => { document.title = `${auth.site.brand_name} — AI API` }, [auth.site.brand_name])

  const logout = useCallback(async () => {
    try { await api.post('/portal/auth/logout') } catch { /* the session is cleared anyway */ }
    setAuth((a) => ({ ...a, user: null }))
  }, [])

  return (
    <AuthCtx.Provider value={{ ...auth, refresh, logout }}>
      <LangProvider defaultLang={auth.site.default_lang}>
      <ToastProvider>
        <Routes>
          <Route element={<PublicLayout />}>
            <Route path="/" element={<Landing />} />
            <Route path="/docs" element={<Docs />} />
            <Route path="/status" element={<Status />} />
            <Route path="/samples" element={<Samples />} />
            <Route path="/compare" element={<Compare />} />
            <Route path="/privacy" element={<Privacy />} />
            <Route path="/terms" element={<Terms />} />
            {/* Legacy Uzbek paths — redirects. Kept because they are registered
                externally (Google console, shared links). */}
            <Route path="/taqqoslash" element={<Navigate to="/compare" replace />} />
            <Route path="/maxfiylik" element={<Navigate to="/privacy" replace />} />
            <Route path="/shartlar" element={<Navigate to="/terms" replace />} />

          </Route>
          <Route path="/login" element={<Login />} />
          <Route path="/reset-password" element={<PasswordReset />} />
          <Route path="/parolni-tiklash" element={<Navigate to="/reset-password" replace />} />
          <Route path="/app" element={<RequireAuth />}>
            <Route element={<Shell />}>
              <Route index element={<Dashboard />} />
              <Route path="keys" element={<Keys />} />
              <Route path="catalog" element={<Catalog />} />
              <Route path="catalog/:id" element={<ModelDetail />} />
              <Route path="usage" element={<Usage />} />
              <Route path="billing" element={<Billing />} />
              <Route path="referral" element={<Referral />} />
              <Route path="docs" element={<Docs inApp />} />
              <Route path="account" element={<Account />} />
              <Route path="admin" element={<RequireAdmin />}>
                <Route index element={<AdminOverview />} />
                <Route path="monitoring" element={<AdminMonitoring />} />
                <Route path="instances" element={<AdminInstances />} />
                <Route path="vnc/:n" element={<AdminVnc />} />
                <Route path="users" element={<AdminUsers />} />
                <Route path="topups" element={<AdminTopups />} />
                <Route path="referrals" element={<AdminReferrals />} />
                <Route path="models" element={<AdminModels />} />
                <Route path="settings" element={<AdminSettings />} />
              </Route>
            </Route>
          </Route>
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </ToastProvider>
      </LangProvider>
    </AuthCtx.Provider>
  )
}
