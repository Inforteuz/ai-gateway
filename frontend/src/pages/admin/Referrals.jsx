import { translateText as tr, useLang as useLocaleRefresh } from '../../i18n/index.jsx'
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
  useLocaleRefresh()
  const toast = useToast()
  const [f, setF] = useState({ enabled: data.enabled, percent: data.percent })
  const [busy, setBusy] = useState(false)
  useEffect(() => { setF({ enabled: data.enabled, percent: data.percent }) }, [data.enabled, data.percent])
  const save = async (e) => {
    e.preventDefault(); setBusy(true)
    try {
      await api.put('/portal/admin/referrals/settings', f)
      toast(tr("Saqlandi")); onSaved()
    } catch (err) { toast(err.message, 'error') } finally { setBusy(false) }
  }
  return (
    <form className="panel" onSubmit={save}>
      <div className="panel-head">
        <span className="ico-chip"><IconSparkles /></span>
        <div className="panel-head__t" style={{ flex: 1 }}>
          <h2>{tr("Dastur sozlamalari")}</h2>
          <p>{tr("Taklif qilingan foydalanuvchining har bir depozitidan (tasdiqlangan to'ldirish yoki \"To'ldirish\" turidagi qo'lda kirim) taklifchiga foiz. Pastga, 1 mikro-so'mgacha yaxlitlanadi.")}</p>
        </div>
      </div>
      <div className="hstack" style={{ gap: 18, alignItems: 'end' }}>
        <label className="check"><input type="checkbox" checked={f.enabled} onChange={(e) => setF((x) => ({ ...x, enabled: e.target.checked }))} /> {tr("Dastur yoqilgan")}</label>
        <label className="field" style={{ width: 160 }}>{tr("Foiz")} <span className="suffix-field">
            <input className="input" inputMode="decimal" required value={f.percent} onChange={(e) => setF((x) => ({ ...x, percent: e.target.value }))} />
            <span className="suffix">%</span>
          </span>
        </label>
        <button className="btn btn--primary" disabled={busy}>{tr(busy ? tr("Saqlanmoqda...") : tr("Saqlash"))}</button>
      </div>
      {!data.enabled && <p className="dim small" style={{ marginTop: 10 }}>{tr("O'chiq: yangi bog'lanish ham, mukofot ham yo'q. Qayta yoqilganda o'tgan depozitlar uchun to'lanmaydi.")}</p>}
    </form>
  )
}

export default function Referrals() {
  useLocaleRefresh()
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
      toast(item.referrer_paused ? tr("Mukofot qayta yoqildi") : tr("Mukofot to'xtatildi")); links.reload()
    } catch (err) { toast(err.message, 'error') }
  }

  return (
    <>
      <div className="page-head">
        <div><h1>{tr("Referallar")}</h1><p>{tr("Kim kimni taklif qilgan, to'langan mukofotlar va shubhali bog'lanishlar.")}</p></div>
      </div>
      <ErrorNote error={links.error} retry={links.reload} />
      {!d && !links.error && <Loading />}
      {d && (
        <>
          <ProgramSettings data={d} onSaved={links.reload} />
          <div className="metric-grid" style={{ marginTop: 18 }}>
            <StatCard icon={<IconUsers />} label={tr("Bog'lanishlar")} value={fmtInt(d.totals.links)} note={tr("{v0} ta taklifchi", { v0: (fmtInt(d.totals.referrers)) })} />
            <StatCard icon={<IconCoins />} label={tr("To'langan mukofot")} value={fmtSom(d.totals.paid, { unit: false })} unit="so'm" tone="amber"
                      note={tr("{v0} ta to'lov{v1}", { v0: (fmtInt(d.totals.paid_count)), v1: (d.totals.skipped_count ? tr(", {v0} ta to'xtatilgan", { v0: (fmtInt(d.totals.skipped_count)) }) : '') })} />
          </div>

          <div className="panel block">
            <div className="panel-head" style={{ flexWrap: 'wrap', gap: 12 }}>
              <div className="panel-head__t" style={{ flex: '1 1 220px' }}><h2>{tr("Kim kimni taklif qilgan")}</h2><p>{tr("Belgilar avtomatik bloklamaydi — shubhali bo'lsa, taklifchiga mukofotni to'xtating.")}</p></div>
              <input className="input" style={{ flex: '1 1 200px', maxWidth: 280 }} placeholder={tr("Email bo'yicha qidirish")} value={q} onChange={(e) => setQ(e.target.value)} />
              <div className="seg" role="group">
                <button type="button" className={!flagged ? 'is-on' : ''} onClick={() => setFlagged(false)}>{tr("Hammasi")}</button>
                <button type="button" className={flagged ? 'is-on' : ''} onClick={() => setFlagged(true)}>{tr("Belgilanganlar")}</button>
              </div>
            </div>
            {d.items.length === 0 && <Empty title={tr("Bog'lanish yo'q")}>{tr(flagged ? tr("Shubhali bog'lanish topilmadi.") : tr("Hali hech kim taklif havolasi orqali ro'yxatdan o'tmagan."))}</Empty>}
            {d.items.length > 0 && (
              <div className="table-wrap"><table className="table">
                <thead><tr><th>{tr("Sana")}</th><th>{tr("Taklif qilgan")}</th><th>{tr("Taklif qilingan")}</th><th>{tr("Belgilar")}</th><th className="num">{tr("Depozitlar")}</th><th className="num">{tr("Mukofot")}</th><th /></tr></thead>
                <tbody>
                  {d.items.map((i) => (
                    <tr key={i.referee_id}>
                      <td className="muted nowrap">{fmtDateTime(i.created_at)}</td>
                      <td>
                        <b>{tr(i.referrer_email)}</b>
                        {i.referrer_paused && <div><span className="tag tag--warn">{tr("mukofot to'xtatilgan")}</span></div>}
                        {i.referrer_status !== 'active' && <div><span className="tag tag--bad">{tr("bloklangan")}</span></div>}
                      </td>
                      <td><b>{tr(i.referee_email)}</b><div className="dim small">{tr(METHOD[i.method] || i.method)}{tr(i.referee_ip ? ` · ${i.referee_ip}` : '')}</div></td>
                      <td>
                        {i.flags.length === 0 && <span className="dim small">—</span>}
                        <div className="hstack" style={{ gap: 4 }}>
                          {i.flags.map((f) => <span key={f} className={`tag ${FLAG[f]?.[1] || ''}`} title={f === 'multi_ip' ? tr("{v0} ta akkaunt", { v0: (i.ip_accounts) }) : f === 'multi_device' ? tr("{v0} ta akkaunt", { v0: (i.device_accounts) }) : undefined}>{tr(FLAG[f]?.[0] || f)}</span>)}
                        </div>
                      </td>
                      <td className="num nowrap">{fmtSom(i.deposits)}</td>
                      <td className="num nowrap"><b>{fmtSom(i.rewarded)}</b></td>
                      <td className="actions">
                        <button className="btn btn--small" onClick={() => pause(i)}>{tr(i.referrer_paused ? 'Mukofotni yoqish' : tr("Mukofotni to'xtatish"))}</button>
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
        <div className="panel-head"><div className="panel-head__t"><h2>{tr("Mukofotlar")}</h2><p>{tr("Har bir depozit uchun bitta yozuv. \"To'xtatilgan\" — taklifchiga mukofot to'xtatilgani yoki u bloklangani uchun to'lanmagan.")}</p></div></div>
        <ErrorNote error={rewards.error} retry={rewards.reload} />
        {rewards.data && rewards.data.items.length === 0 && <Empty title={tr("Hali mukofot yo'q")}>{tr("Taklif qilinganlar depozit qilganda shu yerda ko'rinadi.")}</Empty>}
        {rewards.data && rewards.data.items.length > 0 && (
          <>
            <div className="table-wrap"><table className="table">
              <thead><tr><th>{tr("Sana")}</th><th>{tr("Taklif qilgan")}</th><th>{tr("Depozit qilgan")}</th><th className="num">{tr("Depozit")}</th><th className="num">{tr("Mukofot")}</th><th>{tr("Holat")}</th></tr></thead>
              <tbody>
                {rewards.data.items.map((r) => (
                  <tr key={r.id}>
                    <td className="muted nowrap">{fmtDateTime(r.created_at)}</td>
                    <td>{tr(r.referrer_email)}</td>
                    <td className="muted">{tr(r.referee_email)}</td>
                    <td className="num muted nowrap">{fmtSom(r.deposit_amount)}</td>
                    <td className="num nowrap"><b>{fmtSom(r.amount)}</b> <span className="dim small">({tr(r.percent)}%)</span></td>
                    <td>{r.status === 'paid' ? <span className="tag tag--ok">{tr("To'landi")}</span> : <span className="tag tag--warn">{tr("To'xtatilgan (")}{tr(SKIP[r.reason] || r.reason)})</span>}</td>
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
