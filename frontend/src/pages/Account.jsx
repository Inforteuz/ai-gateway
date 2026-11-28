import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { useRef, useState } from 'react'
import { api } from '../api.js'
import { useAuth } from '../auth.js'
import UserAvatar, { squareJpeg } from '../components/Avatar.jsx'
import { PageHead, PasswordInput, useToast } from '../components/ui.jsx'

const MAX_PHOTO = 15 * 1024 * 1024

/** Profile photo: pick, crop to a square in the browser, upload; or remove. */
function PhotoField({ user, onChanged }) {
  useLocaleRefresh()
  const toast = useToast()
  const fileRef = useRef(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  async function pick(e) {
    const f = e.target.files?.[0]
    e.target.value = ''
    if (!f) return
    setError('')
    if (!/^image\/(png|jpeg|webp)$/.test(f.type)) { setError(tr('Faqat PNG, JPEG yoki WebP rasm.')); return }
    if (f.size > MAX_PHOTO) { setError(tr('Rasm 15 MB dan oshmasligi kerak.')); return }
    setBusy(true)
    try {
      const image = await squareJpeg(f)
      await api.put('/portal/account/avatar', { image })
      await onChanged()
      toast(tr('Profil rasmi yangilandi.'))
    } catch (err) {
      setError(err.message === 'decode' ? tr('Rasmni o‘qib bo‘lmadi.') : err.message)
    } finally { setBusy(false) }
  }

  async function remove() {
    setBusy(true); setError('')
    try { await api.del('/portal/account/avatar'); await onChanged(); toast(tr('Profil rasmi olib tashlandi.')) }
    catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  return (
    <div className="photo-field">
      <UserAvatar user={user} className="avatar avatar--xl" />
      <div className="photo-field__body">
        <b>{tr('Profil rasmi')}</b>
        <span className="hint">{tr('PNG, JPEG yoki WebP. Rasm kvadrat qilib kesiladi.')}</span>
        <div className="photo-field__actions">
          <button type="button" className="btn btn--small btn--primary" disabled={busy} onClick={() => fileRef.current?.click()}>
            {tr(user.avatar ? 'Rasmni almashtirish' : 'Rasm yuklash')}
          </button>
          {user.avatar && <button type="button" className="btn btn--small" disabled={busy} onClick={remove}>{tr('Olib tashlash')}</button>}
        </div>
        {error && <div className="alert alert--error" role="alert">{error}</div>}
      </div>
      <input ref={fileRef} type="file" accept="image/png,image/jpeg,image/webp" hidden onChange={pick} />
    </div>
  )
}

export default function Account() {
  useLocaleRefresh()
  const { user, refresh } = useAuth()
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
      <div className="account-grid">
      <div className="panel account-card">
        <div className="account-id">
          <h2>{tr("Hisob ma'lumotlari")}</h2>
        </div>
        <PhotoField user={user} onChanged={refresh} />
        <dl className="kv">
          <dt>{tr("Email")}</dt><dd>{tr(user.email)}</dd>
          <dt>{tr("Ism")}</dt><dd>{tr(user.name || '-')}</dd>
          <dt>{tr("Rol")}</dt><dd>{tr(user.role === 'admin' ? tr("Administrator") : tr("Foydalanuvchi"))}</dd>
        </dl>
      </div>
      <div className="panel">
        <div className="panel-head">
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
      </div>
    </>
  )
}
