import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth.js'
import { PageHead, PasswordInput, useToast } from '../components/ui.jsx'
import { IconShield, IconUser } from '../components/icons.jsx'

export default function Account() {
  useLocaleRefresh()
  const { user } = useAuth()
  const toast = useToast()
  const [form, setForm] = useState({ current: '', next: '', again: '' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))

  const submit = async (e) => {
    e.preventDefault(); setError('')
    if (form.next !== form.again) { setError(tr("Yangi parollar bir xil emas.")); return }
    setBusy(true)
    try {
      await api.post('/portal/auth/password', { current: form.current, new: form.next })
      toast(tr("Parol almashtirildi. Boshqa qurilmalardan chiqarildingiz."))
      setForm({ current: '', next: '', again: '' })
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  return (
    <>
      <PageHead eyebrow={tr("Hisob sozlamalari")} title={tr("Profil")} lead={tr("Hisob ma'lumotlaringiz va kirish paroli.")} />
      <div className="panel" style={{ maxWidth: 560 }}>
        <div className="panel-head">
          <span className="ico-chip"><IconUser /></span>
          <div className="panel-head__t" style={{ flex: 1 }}><h2>{tr("Hisob ma'lumotlari")}</h2></div>
        </div>
        <dl className="kv">
          <dt>{tr("Email")}</dt><dd>{tr(user.email)}</dd>
          <dt>{tr("Ism")}</dt><dd>{tr(user.name || '-')}</dd>
          <dt>{tr("Rol")}</dt><dd>{tr(user.role === tr("admin") ? tr("Administrator") : tr("Foydalanuvchi"))}</dd>
        </dl>
      </div>
      <div className="panel" style={{ maxWidth: 560, marginTop: 18 }}>
        <div className="panel-head">
          <span className="ico-chip"><IconShield /></span>
          <div className="panel-head__t" style={{ flex: 1 }}><h2>{tr("Parolni almashtirish")}</h2></div>
        </div>
        <form className="form" onSubmit={submit}>
          {error && <div className="alert alert--error" role="alert">{tr(error)}</div>}
          <label className="field">{tr("Joriy parol")}<PasswordInput required value={form.current} onChange={set('current')} autoComplete="current-password" /></label>
          <label className="field">{tr("Yangi parol")}<PasswordInput required minLength={8} value={form.next} onChange={set('next')} autoComplete="new-password" /><span className="hint">{tr("Kamida 8 ta belgi.")}</span></label>
          <label className="field">{tr("Yangi parolni takrorlang")}<PasswordInput required minLength={8} value={form.again} onChange={set('again')} autoComplete="new-password" /></label>
          <button className="btn btn--primary" disabled={busy}>{tr("Parolni almashtirish")}</button>
        </form>
      </div>
    </>
  )
}
