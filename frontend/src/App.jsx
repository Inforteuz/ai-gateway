import { lazy, Suspense, useCallback, useEffect, useState } from 'react'
import { Navigate, Outlet, Route, Routes, useLocation } from 'react-router-dom'
import { api } from './api.js'
import { AuthCtx, useAuth } from './auth.js'
import { forgetReferral } from './referral.js'
import { normalizeSite } from './site.js'
import { LangProvider } from './i18n/index.jsx'
import { BrandLoader, ToastProvider } from './components/ui.jsx'
const AdminUsage = lazy(() => import('./pages/admin/Usage.jsx'))
const Shell = lazy(() => import('./pages/Shell.jsx'))
const Landing = lazy(() => import('./pages/Landing.jsx'))
const Login = lazy(() => import('./pages/Login.jsx'))
const Docs = lazy(() => import('./pages/Docs.jsx'))
const PublicLayout = lazy(() => import('./pages/PublicLayout.jsx'))
const Dashboard = lazy(() => import('./pages/Dashboard.jsx'))
const Keys = lazy(() => import('./pages/Keys.jsx'))
const Usage = lazy(() => import('./pages/Usage.jsx'))
const Billing = lazy(() => import('./pages/Billing.jsx'))
const Account = lazy(() => import('./pages/Account.jsx'))
const Catalog = lazy(() => import('./pages/Catalog.jsx'))
const ModelDetail = lazy(() => import('./pages/ModelDetail.jsx'))
const AdminOverview = lazy(() => import('./pages/admin/Overview.jsx'))
const AdminUsers = lazy(() => import('./pages/admin/Users.jsx'))
const AdminTopups = lazy(() => import('./pages/admin/Topups.jsx'))
const AdminModels = lazy(() => import('./pages/admin/Models.jsx'))
const AdminSettings = lazy(() => import('./pages/admin/Settings.jsx'))
const AdminMonitoring = lazy(() => import('./pages/admin/Monitoring.jsx'))
const AdminReferrals = lazy(() => import('./pages/admin/Referrals.jsx'))
const AdminInstances = lazy(() => import('./pages/admin/Instances.jsx'))
const AdminVnc = lazy(() => import('./pages/admin/Vnc.jsx'))
const Referral = lazy(() => import('./pages/Referral.jsx'))
const Status = lazy(() => import('./pages/Status.jsx'))
const Samples = lazy(() => import('./pages/Samples.jsx'))
const Compare = lazy(() => import('./pages/Compare.jsx'))
const PasswordReset = lazy(() => import('./pages/PasswordReset.jsx'))
const Privacy = lazy(() => import('./pages/Privacy.jsx'))
const Terms = lazy(() => import('./pages/Terms.jsx'))

// Only for the very first load, before any layout exists. Navigation inside a
// layout waits in that layout's own boundary (components/RouteOutlet.jsx).
function RouteLoading() {
  return <BrandLoader full />
}

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
        <Suspense fallback={<RouteLoading />}>
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
                <Route path="usage" element={<AdminUsage />} />
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
        </Suspense>
      </ToastProvider>
      </LangProvider>
    </AuthCtx.Provider>
  )
}
