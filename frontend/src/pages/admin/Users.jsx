import { translateText as tr, useLang as useLocaleRefresh } from '../../i18n/index.jsx'
import { useEffect, useState } from 'react'
import { api, qs } from '../../api.js'
import { useAuth } from '../../auth.js'
import Tape from '../../components/Tape.jsx'
import { CopyButton, Empty, ErrorNote, Loading, Modal, useLoad, useToast } from '../../components/ui.jsx'
import { fmtAgo, fmtDateTime, fmtSom, somToMicro } from '../../format.js'
import { LevelSelect } from '../../reasoning.jsx'

const KINDS = [['topup', 'To\'ldirish (+)'], ['bonus', 'Sovg\'a (+)'], ['refund', 'Qaytarish (+)'], ['adjustment', 'Tuzatish (+ yoki -)']]
const LEDGER_KIND = { topup: 'To\'ldirish', usage: 'So\'rov', adjustment: 'Tuzatish', refund: 'Qaytarish', bonus: 'Sovg\'a', referral: 'Taklif mukofoti' }

function Secret({ title, email, password, onClose }) {
  useLocaleRefresh()
  return (
    <Modal title={tr(title)} onClose={onClose} actions={<button className="btn btn--primary" onClick={onClose}>{tr("Yozib oldim")}</button>}>
      <p className="muted">{tr(email)} {tr("uchun vaqtinchalik parol. U faqat hozir ko'rsatiladi. Foydalanuvchiga yuboring va kirgach almashtirishini ayting.")}</p>
      <div className="keybox"><code>{password}</code><CopyButton text={password} small={false} /></div>
    </Modal>
  )
}

function AddUser({ onClose, onDone, onSecret }) {
  useLocaleRefresh()
  const [f, setF] = useState({ email: '', name: '', credit: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }))
  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setError('')
    try {
      const r = await api.post('/portal/admin/users', { email: f.email, name: f.name, initial_credit: f.credit ? somToMicro(f.credit) : 0 })
      onDone(); onClose()
      if (r.temp_password) onSecret({ title: tr("Foydalanuvchi yaratildi"), email: r.item.email, password: r.temp_password })
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return (
    <Modal title={tr("Foydalanuvchi qo'shish")} onClose={onClose} actions={<>
      <button className="btn" onClick={onClose}>{tr("Bekor qilish")}</button>
      <button className="btn btn--primary" form="add-user" disabled={busy}>{tr("Yaratish")}</button>
    </>}>
      <form id="add-user" className="form" onSubmit={submit}>
        {error && <div className="alert alert--error" role="alert">{tr(error)}</div>}
        <label className="field">{tr("Email")}<input className="input" type="email" required value={f.email} onChange={set('email')} autoFocus /></label>
        <label className="field">{tr("Ism")}<input className="input" value={f.name} onChange={set('name')} maxLength={80} /></label>
        <label className="field">{tr("Boshlang'ich mablag', so'm (ixtiyoriy)")}<input className="input" type="number" min="0" value={f.credit} onChange={set('credit')} /></label>
        <p className="hint">{tr("Parol avtomatik yaratiladi va keyingi oynada bir marta ko'rsatiladi.")}</p>
      </form>
    </Modal>
  )
}

function Detail({ id, onClose, onChanged, onSecret }) {
  useLocaleRefresh()
  const { user: me } = useAuth()
  const toast = useToast()
  const { data, error, reload } = useLoad(() => api.get(`/portal/admin/users/${id}`), [id])
  const [fund, setFund] = useState({ kind: 'topup', amount: '', note: '' })
  const [busy, setBusy] = useState(false)
  const u = data?.item
  const done = () => { reload(); onChanged() }
  const run = async (fn, ok) => {
    setBusy(true)
    try { await fn(); if (ok) toast(ok); done() } catch (err) { toast(err.message, 'error') } finally { setBusy(false) }
  }
  const addFunds = (e) => {
    e.preventDefault()
    const micro = somToMicro(fund.amount)
    run(() => api.post(`/portal/admin/users/${id}/credit`, { amount: micro, kind: fund.kind, note: fund.note }), tr("Hisob yangilandi"))
      .then(() => setFund((f) => ({ ...f, amount: '', note: '' })))
  }
  return (
    <Modal title={u ? (u.name || u.email) : tr("Foydalanuvchi")} wide onClose={onClose}>
      <ErrorNote error={error} retry={reload} />
      {!data && !error && <Loading />}
      {u && (
        <>
          <dl className="kv">
            <dt>{tr("Email")}</dt><dd>{tr(u.email)} {u.role === 'admin' && <span className="tag">{tr("administrator")}</span>} {u.status !== 'active' && <span className="tag tag--bad">{tr("to'xtatilgan")}</span>}</dd>
            <dt>{tr("Hisob")}</dt><dd><b>{fmtSom(u.balance)}</b>{u.held > 0 && <span className="dim"> {tr("(band:")} {fmtSom(u.held)})</span>}</dd>
            <dt>{tr("Shu oy")}</dt><dd>{fmtSom(u.month_cost)}, {tr(u.month_requests)} {tr("ta so'rov")}</dd>
            <dt>{tr("Ro'yxatdan o'tgan")}</dt><dd>{fmtDateTime(u.created_at)}{tr(", oxirgi kirish:")} {fmtAgo(u.last_login_at)}</dd>
          </dl>

          <form className="form form--row" onSubmit={addFunds}>
            <label className="field">{tr("Amal")} <select className="input" value={fund.kind} onChange={(e) => setFund({ ...fund, kind: e.target.value })}>{KINDS.map(([k, l]) => <option key={k} value={k}>{tr(l)}</option>)}</select>
            </label>
            <label className="field">{tr("Summa, so'm")}<input className="input" type="number" step="any" required value={fund.amount} onChange={(e) => setFund({ ...fund, amount: e.target.value })} /></label>
            <label className="field">{tr("Izoh")}<input className="input" value={fund.note} onChange={(e) => setFund({ ...fund, note: e.target.value })} maxLength={300} /></label>
            <button className="btn btn--primary" disabled={busy || !fund.amount}>{tr("Qo'llash")}</button>
          </form>

          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            <button className="btn btn--small" disabled={busy} onClick={() => run(() => api.patch(`/portal/admin/users/${id}`, { status: u.status === 'active' ? 'suspended' : 'active' }), tr("Holat o'zgardi"))}>
              {tr(u.status === 'active' ? tr("To'xtatish") : tr("Qayta faollashtirish"))}
            </button>
            <button className="btn btn--small" disabled={busy} onClick={() => run(async () => { const r = await api.post(`/portal/admin/users/${id}/reset-password`); onSecret({ title: tr("Yangi parol"), email: u.email, password: r.temp_password }) })}>{tr("Parolni tiklash")}</button>
            {u.id !== me.id && <button className="btn btn--small" disabled={busy} onClick={() => run(() => api.patch(`/portal/admin/users/${id}`, { role: u.role === 'admin' ? 'user' : 'admin' }), tr("Rol o'zgardi"))}>{tr(u.role === 'admin' ? 'Administratorlikdan olish' : 'Administrator qilish')}</button>}
            {/* Archiving, not deleting: the ledger and usage rows behind an account have to stay */}
            {u.id !== me.id && u.status !== 'archived' && (
              <button className="btn btn--small btn--danger" disabled={busy} onClick={() => {
                if (!window.confirm(tr("{v0} arxivlansinmi? Akkaunt ro'yxatdan yo'qoladi, barcha kalitlari bekor qilinadi va sessiyalari uziladi. Hisob tarixi va balansi saqlanib qoladi — keyin qaytarish mumkin.", { v0: (u.email) }))) return
                run(() => api.post(`/portal/admin/users/${id}/archive`), 'Akkaunt arxivlandi')
              }}>{tr("Arxivlash")}</button>
            )}
            {u.status === 'archived' && (
              <button className="btn btn--small" disabled={busy} onClick={() => run(() => api.post(`/portal/admin/users/${id}/restore`), tr("Akkaunt qaytarildi"))}>{tr("Arxivdan qaytarish")}</button>
            )}
          </div>

          <div>
            <h3 style={{ marginBottom: 8 }}>{tr("Kalitlar")}</h3>
            {data.keys.length === 0 ? <p className="muted">{tr("Kalit yo'q.")}</p> : (
              <div className="table-wrap"><table className="table"><tbody>
                {data.keys.map((k) => (
                  <tr key={k.id}>
                    <td><b>{tr(k.name)}</b> <code className="dim">{k.prefix}...{k.last4}</code> {k.status !== 'active' && <span className="tag tag--bad">{tr("bekor")}</span>} {k.free && <span className="tag tag--warn">{tr("bepul")}</span>}</td>
                    <td className="nowrap">{tr(k.status === 'active' ? <span style={{ display: 'inline-block', minWidth: 190 }}><LevelSelect value={k.reasoning_effort || 'inherit'} onChange={(v) => run(() => api.patch(`/portal/admin/keys/${k.id}`, { reasoning_effort: v }), tr("Fikrlash darajasi o'zgardi"))} /></span> : null)}</td>
                    <td className="num nowrap">{fmtSom(k.month_spent)}</td>
                    <td className="actions">
                      {k.status === 'active' && <>
                        <button className="btn btn--small" disabled={busy} onClick={() => run(() => api.patch(`/portal/admin/keys/${k.id}`, { free: !k.free }), k.free ? 'Pullik qilindi' : 'Bepul qilindi')}>{k.free ? 'Pullik qilish' : 'Bepul qilish'}</button>
                        <button className="btn btn--small btn--danger" disabled={busy} onClick={() => run(() => api.post(`/portal/admin/keys/${k.id}/revoke`), tr("Kalit bekor qilindi"))}>{tr("Bekor qilish")}</button>
                      </>}
                    </td>
                  </tr>
                ))}
              </tbody></table></div>
            )}
          </div>

          <div><h3 style={{ marginBottom: 10 }}>{tr("Oxirgi so'rovlar")}</h3><Tape items={data.usage} empty={tr("So'rov yo'q.")} /></div>
          <div style={{ marginTop: 6 }}>
            <h3 style={{ marginBottom: 8 }}>{tr("Hisob tarixi")}</h3>
            <div className="table-wrap"><table className="table"><tbody>
              {data.ledger.map((l) => (
                <tr key={l.id}><td className="muted nowrap">{fmtDateTime(l.created_at)}</td><td>{tr(LEDGER_KIND[l.kind])}</td><td className="muted">{tr(l.note)}</td><td className="num"><b>{fmtSom(l.amount, { sign: true })}</b></td><td className="num muted">{fmtSom(l.balance_after)}</td></tr>
              ))}
            </tbody></table></div>
          </div>
          <div className="modal-actions"><button className="btn" onClick={onClose}>{tr("Yopish")}</button></div>
        </>
      )}
    </Modal>
  )
}

export default function Users() {
  useLocaleRefresh()
  const [q, setQ] = useState('')
  const [dq, setDq] = useState('')
  useEffect(() => { const t = setTimeout(() => setDq(q), 250); return () => clearTimeout(t) }, [q])
  const [archived, setArchived] = useState(false)
  const { data, error, reload } = useLoad(() => api.get('/portal/admin/users' + qs({ q: dq, archived: archived ? 1 : '' })), [dq, archived])
  const [adding, setAdding] = useState(false)
  const [open, setOpen] = useState(null)
  const [secret, setSecret] = useState(null)
  return (
    <>
      <div className="page-head">
        <div><h1>{tr("Foydalanuvchilar")}</h1><p>{tr("Hamkor uchun hisob oching, hisobini to'ldiring va kalitlarini boshqaring.")}</p></div>
        <button className="btn btn--primary" onClick={() => setAdding(true)}>{tr("Foydalanuvchi qo'shish")}</button>
      </div>
      <div className="hstack" style={{ gap: 10, marginBottom: 18, flexWrap: 'wrap' }}>
        <input className="input" style={{ maxWidth: 340 }} placeholder={tr("Email yoki ism bo'yicha qidirish")} value={q} onChange={(e) => setQ(e.target.value)} aria-label={tr("Qidirish")} />
        <button className="btn btn--small" onClick={() => setArchived(!archived)}>
          {tr(archived ? tr("Faol akkauntlar") : 'Arxivlanganlar')}
        </button>
      </div>
      <ErrorNote error={error} retry={reload} />
      {!data && !error && <Loading rows={4} />}
      {data && data.items.length === 0 && <Empty title={tr("Hech kim topilmadi")}>{tr("Boshqa so'z bilan qidirib ko'ring yoki yangi foydalanuvchi qo'shing.")}</Empty>}
      {data && data.items.length > 0 && (
        <div className="panel table-wrap"><table className="table">
          <thead><tr><th>{tr("Foydalanuvchi")}</th><th className="num">{tr("Hisob")}</th><th className="num">{tr("Shu oy")}</th><th className="num">{tr("Kalit")}</th><th>{tr("Oxirgi so'rov")}</th></tr></thead>
          <tbody>
            {data.items.map((u) => (
              <tr key={u.id} className="is-click" onClick={() => setOpen(u.id)} tabIndex={0} onKeyDown={(e) => e.key === 'Enter' && setOpen(u.id)}>
                <td><b>{tr(u.name || u.email)}</b> {u.role === 'admin' && <span className="tag">{tr("admin")}</span>} {u.status === 'archived' ? <span className="tag tag--bad">{tr("arxivlangan")}</span> : u.status !== 'active' && <span className="tag tag--bad">{tr("to'xtatilgan")}</span>}<div className="dim small">{tr(u.email)}</div></td>
                <td className="num"><b>{fmtSom(u.balance)}</b></td>
                <td className="num muted">{fmtSom(u.month_cost)}</td>
                <td className="num muted">{tr(u.active_keys)}</td>
                <td className="muted nowrap" style={{ paddingLeft: 18 }}>{fmtAgo(u.last_request_at)}</td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
      {adding && <AddUser onClose={() => setAdding(false)} onDone={reload} onSecret={setSecret} />}
      {open && <Detail id={open} onClose={() => setOpen(null)} onChanged={reload} onSecret={setSecret} />}
      {secret && <Secret {...secret} onClose={() => setSecret(null)} />}
    </>
  )
}
