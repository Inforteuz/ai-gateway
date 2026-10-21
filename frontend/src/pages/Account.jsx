import { useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth.js'
import { PageHead, PasswordInput, useToast } from '../components/ui.jsx'
import { IconShield, IconUser } from '../components/icons.jsx'

export default function Account() {
  const { user } = useAuth()
  const toast = useToast()
  const [form, setForm] = useState({ current: '', next: '', again: '' })
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const set = (k) => (e) => setForm((f) => ({ ...f, [k]: e.target.value }))

  const submit = async (e) => {
    e.preventDefault(); setError('')
    if (form.next !== form.again) { setError('Yangi parollar bir xil emas.'); return }
    setBusy(true)
    try {
      await api.post('/portal/auth/password', { current: form.current, new: form.next })
      toast('Parol almashtirildi. Boshqa qurilmalardan chiqarildingiz.')
      setForm({ current: '', next: '', again: '' })
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  return (
    <>
      <PageHead eyebrow="Hisob sozlamalari" title="Profil" lead="Hisob ma'lumotlaringiz va kirish paroli." />
      <div className="panel" style={{ maxWidth: 560 }}>
        <div className="panel-head">
          <span className="ico-chip"><IconUser /></span>
          <div className="panel-head__t" style={{ flex: 1 }}><h2>Hisob ma'lumotlari</h2></div>
        </div>
        <dl className="kv">
          <dt>Email</dt><dd>{user.email}</dd>
          <dt>Ism</dt><dd>{user.name || '-'}</dd>
          <dt>Rol</dt><dd>{user.role === 'admin' ? 'Administrator' : 'Foydalanuvchi'}</dd>
        </dl>
      </div>
      <div className="panel" style={{ maxWidth: 560, marginTop: 18 }}>
        <div className="panel-head">
          <span className="ico-chip"><IconShield /></span>
          <div className="panel-head__t" style={{ flex: 1 }}><h2>Parolni almashtirish</h2></div>
        </div>
        <form className="form" onSubmit={submit}>
          {error && <div className="alert alert--error" role="alert">{error}</div>}
          <label className="field">Joriy parol<PasswordInput required value={form.current} onChange={set('current')} autoComplete="current-password" /></label>
          <label className="field">Yangi parol<PasswordInput required minLength={8} value={form.next} onChange={set('next')} autoComplete="new-password" /><span className="hint">Kamida 8 ta belgi.</span></label>
          <label className="field">Yangi parolni takrorlang<PasswordInput required minLength={8} value={form.again} onChange={set('again')} autoComplete="new-password" /></label>
          <button className="btn btn--primary" disabled={busy}>Parolni almashtirish</button>
        </form>
      </div>
    </>
  )
}
