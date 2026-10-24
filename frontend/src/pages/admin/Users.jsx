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
  return (
    <Modal title={title} onClose={onClose} actions={<button className="btn btn--primary" onClick={onClose}>Yozib oldim</button>}>
      <p className="muted">{email} uchun vaqtinchalik parol. U faqat hozir ko'rsatiladi. Foydalanuvchiga yuboring va kirgach almashtirishini ayting.</p>
      <div className="keybox"><code>{password}</code><CopyButton text={password} small={false} /></div>
    </Modal>
  )
}

function AddUser({ onClose, onDone, onSecret }) {
  const [f, setF] = useState({ email: '', name: '', credit: '' })
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.value }))
  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setError('')
    try {
      const r = await api.post('/portal/admin/users', { email: f.email, name: f.name, initial_credit: f.credit ? somToMicro(f.credit) : 0 })
      onDone(); onClose()
      if (r.temp_password) onSecret({ title: 'Foydalanuvchi yaratildi', email: r.item.email, password: r.temp_password })
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }
  return (
    <Modal title="Foydalanuvchi qo'shish" onClose={onClose} actions={<>
      <button className="btn" onClick={onClose}>Bekor qilish</button>
      <button className="btn btn--primary" form="add-user" disabled={busy}>Yaratish</button>
    </>}>
      <form id="add-user" className="form" onSubmit={submit}>
        {error && <div className="alert alert--error" role="alert">{error}</div>}
        <label className="field">Email<input className="input" type="email" required value={f.email} onChange={set('email')} autoFocus /></label>
        <label className="field">Ism<input className="input" value={f.name} onChange={set('name')} maxLength={80} /></label>
        <label className="field">Boshlang'ich mablag', so'm (ixtiyoriy)<input className="input" type="number" min="0" value={f.credit} onChange={set('credit')} /></label>
        <p className="hint">Parol avtomatik yaratiladi va keyingi oynada bir marta ko'rsatiladi.</p>
      </form>
    </Modal>
  )
}

function Detail({ id, onClose, onChanged, onSecret }) {
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
    run(() => api.post(`/portal/admin/users/${id}/credit`, { amount: micro, kind: fund.kind, note: fund.note }), 'Hisob yangilandi')
      .then(() => setFund((f) => ({ ...f, amount: '', note: '' })))
  }
  return (
    <Modal title={u ? (u.name || u.email) : 'Foydalanuvchi'} wide onClose={onClose}>
      <ErrorNote error={error} retry={reload} />
      {!data && !error && <Loading />}
      {u && (
        <>
          <dl className="kv">
            <dt>Email</dt><dd>{u.email} {u.role === 'admin' && <span className="tag">administrator</span>} {u.status !== 'active' && <span className="tag tag--bad">to'xtatilgan</span>}</dd>
            <dt>Hisob</dt><dd><b>{fmtSom(u.balance)}</b>{u.held > 0 && <span className="dim"> (band: {fmtSom(u.held)})</span>}</dd>
            <dt>Shu oy</dt><dd>{fmtSom(u.month_cost)}, {u.month_requests} ta so'rov</dd>
            <dt>Ro'yxatdan o'tgan</dt><dd>{fmtDateTime(u.created_at)}, oxirgi kirish: {fmtAgo(u.last_login_at)}</dd>
          </dl>

          <form className="form form--row" onSubmit={addFunds}>
            <label className="field">Amal
              <select className="input" value={fund.kind} onChange={(e) => setFund({ ...fund, kind: e.target.value })}>{KINDS.map(([k, l]) => <option key={k} value={k}>{l}</option>)}</select>
            </label>
            <label className="field">Summa, so'm<input className="input" type="number" step="any" required value={fund.amount} onChange={(e) => setFund({ ...fund, amount: e.target.value })} /></label>
            <label className="field">Izoh<input className="input" value={fund.note} onChange={(e) => setFund({ ...fund, note: e.target.value })} maxLength={300} /></label>
            <button className="btn btn--primary" disabled={busy || !fund.amount}>Qo'llash</button>
          </form>

          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            <button className="btn btn--small" disabled={busy} onClick={() => run(() => api.patch(`/portal/admin/users/${id}`, { status: u.status === 'active' ? 'suspended' : 'active' }), 'Holat o\'zgardi')}>
              {u.status === 'active' ? 'To\'xtatish' : 'Qayta faollashtirish'}
            </button>
            <button className="btn btn--small" disabled={busy} onClick={() => run(async () => { const r = await api.post(`/portal/admin/users/${id}/reset-password`); onSecret({ title: 'Yangi parol', email: u.email, password: r.temp_password }) })}>Parolni tiklash</button>
            {u.id !== me.id && <button className="btn btn--small" disabled={busy} onClick={() => run(() => api.patch(`/portal/admin/users/${id}`, { role: u.role === 'admin' ? 'user' : 'admin' }), 'Rol o\'zgardi')}>{u.role === 'admin' ? 'Administratorlikdan olish' : 'Administrator qilish'}</button>}
            {/* Archiving, not deleting: the ledger and usage rows behind an account have to stay */}
            {u.id !== me.id && u.status !== 'archived' && (
              <button className="btn btn--small btn--danger" disabled={busy} onClick={() => {
                if (!window.confirm(`${u.email} arxivlansinmi?\n\nAkkaunt ro'yxatdan yo'qoladi, barcha kalitlari bekor qilinadi va sessiyalari uziladi. Hisob tarixi va balansi saqlanib qoladi — keyin qaytarish mumkin.`)) return
                run(() => api.post(`/portal/admin/users/${id}/archive`), 'Akkaunt arxivlandi')
              }}>Arxivlash</button>
            )}
            {u.status === 'archived' && (
              <button className="btn btn--small" disabled={busy} onClick={() => run(() => api.post(`/portal/admin/users/${id}/restore`), 'Akkaunt qaytarildi')}>Arxivdan qaytarish</button>
            )}
          </div>

          <div>
            <h3 style={{ marginBottom: 8 }}>Kalitlar</h3>
            {data.keys.length === 0 ? <p className="muted">Kalit yo'q.</p> : (
              <div className="table-wrap"><table className="table"><tbody>
                {data.keys.map((k) => (
                  <tr key={k.id}>
                    <td><b>{k.name}</b> <code className="dim">{k.prefix}...{k.last4}</code> {k.status !== 'active' && <span className="tag tag--bad">bekor</span>} {k.free && <span className="tag tag--warn">bepul</span>}</td>
                    <td className="nowrap">{k.status === 'active' ? <span style={{ display: 'inline-block', minWidth: 190 }}><LevelSelect value={k.reasoning_effort || 'inherit'} onChange={(v) => run(() => api.patch(`/portal/admin/keys/${k.id}`, { reasoning_effort: v }), 'Fikrlash darajasi o\'zgardi')} /></span> : null}</td>
                    <td className="num nowrap">{fmtSom(k.month_spent)}</td>
                    <td className="actions">
                      {k.status === 'active' && <>
                        <button className="btn btn--small" disabled={busy} onClick={() => run(() => api.patch(`/portal/admin/keys/${k.id}`, { free: !k.free }), k.free ? 'Pullik qilindi' : 'Bepul qilindi')}>{k.free ? 'Pullik qilish' : 'Bepul qilish'}</button>
                        <button className="btn btn--small btn--danger" disabled={busy} onClick={() => run(() => api.post(`/portal/admin/keys/${k.id}/revoke`), 'Kalit bekor qilindi')}>Bekor qilish</button>
                      </>}
                    </td>
                  </tr>
                ))}
              </tbody></table></div>
            )}
          </div>

          <div><h3 style={{ marginBottom: 10 }}>Oxirgi so'rovlar</h3><Tape items={data.usage} empty="So'rov yo'q." /></div>
          <div style={{ marginTop: 6 }}>
            <h3 style={{ marginBottom: 8 }}>Hisob tarixi</h3>
            <div className="table-wrap"><table className="table"><tbody>
              {data.ledger.map((l) => (
                <tr key={l.id}><td className="muted nowrap">{fmtDateTime(l.created_at)}</td><td>{LEDGER_KIND[l.kind]}</td><td className="muted">{l.note}</td><td className="num"><b>{fmtSom(l.amount, { sign: true })}</b></td><td className="num muted">{fmtSom(l.balance_after)}</td></tr>
              ))}
            </tbody></table></div>
          </div>
          <div className="modal-actions"><button className="btn" onClick={onClose}>Yopish</button></div>
        </>
      )}
    </Modal>
  )
}

export default function Users() {
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
        <div><h1>Foydalanuvchilar</h1><p>Hamkor uchun hisob oching, hisobini to'ldiring va kalitlarini boshqaring.</p></div>
        <button className="btn btn--primary" onClick={() => setAdding(true)}>Foydalanuvchi qo'shish</button>
      </div>
      <div className="hstack" style={{ gap: 10, marginBottom: 18, flexWrap: 'wrap' }}>
        <input className="input" style={{ maxWidth: 340 }} placeholder="Email yoki ism bo'yicha qidirish" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Qidirish" />
        <button className="btn btn--small" onClick={() => setArchived(!archived)}>
          {archived ? 'Faol akkauntlar' : 'Arxivlanganlar'}
        </button>
      </div>
      <ErrorNote error={error} retry={reload} />
      {!data && !error && <Loading rows={4} />}
      {data && data.items.length === 0 && <Empty title="Hech kim topilmadi">Boshqa so'z bilan qidirib ko'ring yoki yangi foydalanuvchi qo'shing.</Empty>}
      {data && data.items.length > 0 && (
        <div className="panel table-wrap"><table className="table">
          <thead><tr><th>Foydalanuvchi</th><th className="num">Hisob</th><th className="num">Shu oy</th><th className="num">Kalit</th><th>Oxirgi so'rov</th></tr></thead>
          <tbody>
            {data.items.map((u) => (
              <tr key={u.id} className="is-click" onClick={() => setOpen(u.id)} tabIndex={0} onKeyDown={(e) => e.key === 'Enter' && setOpen(u.id)}>
                <td><b>{u.name || u.email}</b> {u.role === 'admin' && <span className="tag">admin</span>} {u.status === 'archived' ? <span className="tag tag--bad">arxivlangan</span> : u.status !== 'active' && <span className="tag tag--bad">to'xtatilgan</span>}<div className="dim small">{u.email}</div></td>
                <td className="num"><b>{fmtSom(u.balance)}</b></td>
                <td className="num muted">{fmtSom(u.month_cost)}</td>
                <td className="num muted">{u.active_keys}</td>
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
