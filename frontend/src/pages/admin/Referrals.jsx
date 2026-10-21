import { useEffect, useState } from 'react'
import { api, qs } from '../../api.js'
import { Empty, ErrorNote, Loading, Pager, StatCard, useLoad, useToast } from '../../components/ui.jsx'
import { IconCoins, IconSparkles, IconUsers } from '../../components/icons.jsx'
import { fmtDateTime, fmtInt, fmtSom } from '../../format.js'

const LIMIT = 30
const METHOD = { password: 'Email', google: 'Google', telegram: 'Telegram' }
// Fraud flags: nothing is blocked automatically, they are only shown — the admin decides.
const FLAG = {
  same_ip: ['Taklifchi bilan bir IP', 'tag--bad'],
  same_device: ['Taklifchi bilan bir qurilma', 'tag--bad'],
  multi_ip: ['Shu IP\'dan bir nechta akkaunt', 'tag--warn'],
  multi_device: ['Shu qurilmadan bir nechta akkaunt', 'tag--warn'],
}
const SKIP = { paused: 'to\'xtatilgan', suspended: 'bloklangan' }

function ProgramSettings({ data, onSaved }) {
  const toast = useToast()
  const [f, setF] = useState({ enabled: data.enabled, percent: data.percent })
  const [busy, setBusy] = useState(false)
  useEffect(() => { setF({ enabled: data.enabled, percent: data.percent }) }, [data.enabled, data.percent])
  const save = async (e) => {
    e.preventDefault(); setBusy(true)
    try {
      await api.put('/portal/admin/referrals/settings', f)
      toast('Saqlandi'); onSaved()
    } catch (err) { toast(err.message, 'error') } finally { setBusy(false) }
  }
  return (
    <form className="panel" onSubmit={save}>
      <div className="panel-head">
        <span className="ico-chip"><IconSparkles /></span>
        <div className="panel-head__t" style={{ flex: 1 }}>
          <h2>Dastur sozlamalari</h2>
          <p>Taklif qilingan foydalanuvchining har bir depozitidan (tasdiqlangan to'ldirish yoki "To'ldirish" turidagi qo'lda kirim) taklifchiga foiz. Pastga, 1 mikro-so'mgacha yaxlitlanadi.</p>
        </div>
      </div>
      <div className="hstack" style={{ gap: 18, alignItems: 'end' }}>
        <label className="check"><input type="checkbox" checked={f.enabled} onChange={(e) => setF((x) => ({ ...x, enabled: e.target.checked }))} /> Dastur yoqilgan</label>
        <label className="field" style={{ width: 160 }}>Foiz
          <span className="suffix-field">
            <input className="input" inputMode="decimal" required value={f.percent} onChange={(e) => setF((x) => ({ ...x, percent: e.target.value }))} />
            <span className="suffix">%</span>
          </span>
        </label>
        <button className="btn btn--primary" disabled={busy}>{busy ? 'Saqlanmoqda...' : 'Saqlash'}</button>
      </div>
      {!data.enabled && <p className="dim small" style={{ marginTop: 10 }}>O'chiq: yangi bog'lanish ham, mukofot ham yo'q. Qayta yoqilganda o'tgan depozitlar uchun to'lanmaydi.</p>}
    </form>
  )
}

export default function Referrals() {
  const toast = useToast()
  const [q, setQ] = useState('')
  const [flagged, setFlagged] = useState(false)
  const [offset, setOffset] = useState(0)
  const links = useLoad(() => api.get(`/portal/admin/referrals${qs({ q, flagged: flagged || undefined })}`), [q, flagged], { refreshMs: 30_000 })
  const rewards = useLoad(() => api.get(`/portal/admin/referrals/rewards?limit=${LIMIT}&offset=${offset}`), [offset], { refreshMs: 30_000 })
  const d = links.data

  const pause = async (item) => {
    try {
      await api.post(`/portal/admin/referrals/users/${item.referrer_id}/pause`, { paused: !item.referrer_paused })
      toast(item.referrer_paused ? 'Mukofot qayta yoqildi' : 'Mukofot to\'xtatildi'); links.reload()
    } catch (err) { toast(err.message, 'error') }
  }

  return (
    <>
      <div className="page-head">
        <div><h1>Referallar</h1><p>Kim kimni taklif qilgan, to'langan mukofotlar va shubhali bog'lanishlar.</p></div>
      </div>
      <ErrorNote error={links.error} retry={links.reload} />
      {!d && !links.error && <Loading />}
      {d && (
        <>
          <ProgramSettings data={d} onSaved={links.reload} />
          <div className="metric-grid" style={{ marginTop: 18 }}>
            <StatCard icon={<IconUsers />} label="Bog'lanishlar" value={fmtInt(d.totals.links)} note={`${fmtInt(d.totals.referrers)} ta taklifchi`} />
            <StatCard icon={<IconCoins />} label="To'langan mukofot" value={fmtSom(d.totals.paid, { unit: false })} unit="so'm" tone="amber"
                      note={`${fmtInt(d.totals.paid_count)} ta to'lov${d.totals.skipped_count ? `, ${fmtInt(d.totals.skipped_count)} ta to'xtatilgan` : ''}`} />
          </div>

          <div className="panel block">
            <div className="panel-head" style={{ flexWrap: 'wrap', gap: 12 }}>
              <div className="panel-head__t" style={{ flex: '1 1 220px' }}><h2>Kim kimni taklif qilgan</h2><p>Belgilar avtomatik bloklamaydi — shubhali bo'lsa, taklifchiga mukofotni to'xtating.</p></div>
              <input className="input" style={{ flex: '1 1 200px', maxWidth: 280 }} placeholder="Email bo'yicha qidirish" value={q} onChange={(e) => setQ(e.target.value)} />
              <div className="seg" role="group">
                <button type="button" className={!flagged ? 'is-on' : ''} onClick={() => setFlagged(false)}>Hammasi</button>
                <button type="button" className={flagged ? 'is-on' : ''} onClick={() => setFlagged(true)}>Belgilanganlar</button>
              </div>
            </div>
            {d.items.length === 0 && <Empty title="Bog'lanish yo'q">{flagged ? 'Shubhali bog\'lanish topilmadi.' : 'Hali hech kim taklif havolasi orqali ro\'yxatdan o\'tmagan.'}</Empty>}
            {d.items.length > 0 && (
              <div className="table-wrap"><table className="table">
                <thead><tr><th>Sana</th><th>Taklif qilgan</th><th>Taklif qilingan</th><th>Belgilar</th><th className="num">Depozitlar</th><th className="num">Mukofot</th><th /></tr></thead>
                <tbody>
                  {d.items.map((i) => (
                    <tr key={i.referee_id}>
                      <td className="muted nowrap">{fmtDateTime(i.created_at)}</td>
                      <td>
                        <b>{i.referrer_email}</b>
                        {i.referrer_paused && <div><span className="tag tag--warn">mukofot to'xtatilgan</span></div>}
                        {i.referrer_status !== 'active' && <div><span className="tag tag--bad">bloklangan</span></div>}
                      </td>
                      <td><b>{i.referee_email}</b><div className="dim small">{METHOD[i.method] || i.method}{i.referee_ip ? ` · ${i.referee_ip}` : ''}</div></td>
                      <td>
                        {i.flags.length === 0 && <span className="dim small">—</span>}
                        <div className="hstack" style={{ gap: 4 }}>
                          {i.flags.map((f) => <span key={f} className={`tag ${FLAG[f]?.[1] || ''}`} title={f === 'multi_ip' ? `${i.ip_accounts} ta akkaunt` : f === 'multi_device' ? `${i.device_accounts} ta akkaunt` : undefined}>{FLAG[f]?.[0] || f}</span>)}
                        </div>
                      </td>
                      <td className="num nowrap">{fmtSom(i.deposits)}</td>
                      <td className="num nowrap"><b>{fmtSom(i.rewarded)}</b></td>
                      <td className="actions">
                        <button className="btn btn--small" onClick={() => pause(i)}>{i.referrer_paused ? 'Mukofotni yoqish' : 'Mukofotni to\'xtatish'}</button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table></div>
            )}
          </div>
        </>
      )}

      <div className="panel block">
        <div className="panel-head"><div className="panel-head__t"><h2>Mukofotlar</h2><p>Har bir depozit uchun bitta yozuv. "To'xtatilgan" — taklifchiga mukofot to'xtatilgani yoki u bloklangani uchun to'lanmagan.</p></div></div>
        <ErrorNote error={rewards.error} retry={rewards.reload} />
        {rewards.data && rewards.data.items.length === 0 && <Empty title="Hali mukofot yo'q">Taklif qilinganlar depozit qilganda shu yerda ko'rinadi.</Empty>}
        {rewards.data && rewards.data.items.length > 0 && (
          <>
            <div className="table-wrap"><table className="table">
              <thead><tr><th>Sana</th><th>Taklif qilgan</th><th>Depozit qilgan</th><th className="num">Depozit</th><th className="num">Mukofot</th><th>Holat</th></tr></thead>
              <tbody>
                {rewards.data.items.map((r) => (
                  <tr key={r.id}>
                    <td className="muted nowrap">{fmtDateTime(r.created_at)}</td>
                    <td>{r.referrer_email}</td>
                    <td className="muted">{r.referee_email}</td>
                    <td className="num muted nowrap">{fmtSom(r.deposit_amount)}</td>
                    <td className="num nowrap"><b>{fmtSom(r.amount)}</b> <span className="dim small">({r.percent}%)</span></td>
                    <td>{r.status === 'paid' ? <span className="tag tag--ok">To'landi</span> : <span className="tag tag--warn">To'xtatilgan ({SKIP[r.reason] || r.reason})</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
            <Pager total={rewards.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
          </>
        )}
      </div>
    </>
  )
}
