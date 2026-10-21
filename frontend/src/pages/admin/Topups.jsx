import { useState } from 'react'
import { api } from '../../api.js'
import { Empty, ErrorNote, Loading, Modal, useLoad, useToast } from '../../components/ui.jsx'
import { fmtDateTime, fmtSom, microToSom, somToMicro } from '../../format.js'

function Resolve({ item, action, onClose, onDone }) {
  const toast = useToast()
  const [amount, setAmount] = useState(String(microToSom(item.amount)))
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const approve = action === 'approve'
  const go = async (e) => {
    e.preventDefault(); setBusy(true)
    try {
      await api.post(`/portal/admin/topups/${item.id}/resolve`, { action, amount: approve ? somToMicro(amount) : undefined, admin_note: note })
      toast(approve ? 'Mablag\' hisobga tushirildi' : 'So\'rov rad etildi'); onDone(); onClose()
    } catch (err) { toast(err.message, 'error') } finally { setBusy(false) }
  }
  return (
    <Modal title={approve ? 'So\'rovni tasdiqlash' : 'So\'rovni rad etish'} onClose={onClose} actions={<>
      <button className="btn" onClick={onClose}>Bekor qilish</button>
      <button className={`btn ${approve ? 'btn--primary' : 'btn--danger'}`} form="resolve" disabled={busy}>{approve ? 'Hisobga tushirish' : 'Rad etish'}</button>
    </>}>
      <p className="muted"><b>{item.user_name || item.email}</b> ({item.email}) {fmtSom(item.amount)} so'ragan.{item.note && <> Izoh: "{item.note}"</>}</p>
      <form id="resolve" className="form" onSubmit={go}>
        {approve && <label className="field">Tushiriladigan summa, so'm<input className="input" type="number" min="1" required value={amount} onChange={(e) => setAmount(e.target.value)} /><span className="hint">To'lov boshqa summada kelgan bo'lsa, o'zgartiring.</span></label>}
        <label className="field">Foydalanuvchiga izoh (ixtiyoriy)<input className="input" value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} /></label>
      </form>
    </Modal>
  )
}

export default function Topups() {
  const [status, setStatus] = useState('pending')
  const { data, error, reload } = useLoad(() => api.get(`/portal/admin/topups?status=${status}`), [status], { refreshMs: 20_000 })
  const [act, setAct] = useState(null)
  const label = { approved: 'Tasdiqlangan', rejected: 'Rad etilgan' }
  return (
    <>
      <div className="page-head">
        <div><h1>To'ldirish so'rovlari</h1><p>To'lov kelganini tekshiring, so'ng tasdiqlang. Tasdiqlangan summa darhol foydalanuvchi hisobiga tushadi.</p></div>
        <div className="seg" role="group"><button className={status === 'pending' ? 'is-on' : ''} onClick={() => setStatus('pending')}>Kutilmoqda</button><button className={status === 'all' ? 'is-on' : ''} onClick={() => setStatus('all')}>Hammasi</button></div>
      </div>
      <ErrorNote error={error} retry={reload} />
      {!data && !error && <Loading />}
      {data && data.items.length === 0 && <Empty title="So'rov yo'q">{status === 'pending' ? 'Kutilayotgan to\'ldirish so\'rovlari yo\'q.' : 'Hali so\'rov kelmagan.'}</Empty>}
      {data && data.items.length > 0 && (
        <div className="panel table-wrap"><table className="table">
          <thead><tr><th>Sana</th><th>Foydalanuvchi</th><th>Izoh</th><th className="num">Summa</th><th /></tr></thead>
          <tbody>
            {data.items.map((t) => (
              <tr key={t.id}>
                <td className="muted nowrap">{fmtDateTime(t.created_at)}</td>
                <td><b>{t.user_name || t.email}</b><div className="dim small">{t.email}</div></td>
                <td className="muted">{t.note || '-'}{t.admin_note && <div className="dim small">Admin: {t.admin_note}</div>}</td>
                <td className="num"><b>{fmtSom(t.amount)}</b></td>
                <td className="actions">
                  {t.status === 'pending'
                    ? <><button className="btn btn--small btn--primary" onClick={() => setAct({ item: t, action: 'approve' })}>Tasdiqlash</button><button className="btn btn--small" onClick={() => setAct({ item: t, action: 'reject' })}>Rad etish</button></>
                    : <span className={`tag ${t.status === 'approved' ? 'tag--ok' : 'tag--bad'}`}>{label[t.status]}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table></div>
      )}
      {act && <Resolve {...act} onClose={() => setAct(null)} onDone={reload} />}
    </>
  )
}
