import { useCallback, useEffect, useState } from 'react'
import { api } from '../api.js'
import { Loading, useLoad } from '../components/ui.jsx'
import { IconClock, IconLayers } from '../components/icons.jsx'
import { fmtDateTime, fmtDay, fmtDuration, fmtPct, fmtTime } from '../format.js'

// The status page refreshes itself. While the page is hidden (another tab,
// minimized window) we send no requests, to spare the server and the client's
// battery. It refreshes once immediately when it becomes visible again.
const REFRESH_MS = 30_000

export default function Status() {
  const cur = useLoad(() => api.get('/portal/status'), [])
  const hist = useLoad(() => api.get('/portal/status/history'), [])
  const mods = useLoad(() => api.get('/portal/status/models'), [])
  const catalog = useLoad(() => api.get('/portal/models'), [])
  const c = cur.data, h = hist.data, m = mods.data
  const [updatedAt, setUpdatedAt] = useState(null)

  const refresh = useCallback(() => { cur.reload(); hist.reload(); mods.reload() }, [cur.reload, hist.reload, mods.reload])

  useEffect(() => { if (c) setUpdatedAt(Date.now() / 1000) }, [c])

  useEffect(() => {
    const t = setInterval(() => {
      if (document.visibilityState === 'visible') refresh()
    }, REFRESH_MS)
    const onVisible = () => { if (document.visibilityState === 'visible') refresh() }
    document.addEventListener('visibilitychange', onVisible)
    return () => { clearInterval(t); document.removeEventListener('visibilitychange', onVisible) }
  }, [refresh])
  // Model states: down first, then unknown, then up.
  const STATE_ORDER = { down: 0, unknown: 1, up: 2 }
  const names = Object.fromEntries((catalog.data?.items || []).map((x) => [x.id, x.display_name]))
  const models = [...(m?.items || [])].sort(
    (a, b) => (STATE_ORDER[a.state] ?? 3) - (STATE_ORDER[b.state] ?? 3) || a.model.localeCompare(b.model))
  const downModels = models.filter((x) => x.state === 'down')
  const heading = !c
    ? 'Tekshirilmoqda...'
    : downModels.length > 0
      ? 'Ba\'zi modellar ishlamayapti'
      : c.model_online ? 'Hamma narsa ishlayapti' : 'Model hozir ulanmagan'

  const cls = (d) => (d.uptime == null ? '' : d.uptime >= 99.5 ? 'is-ok' : d.uptime >= 95 ? 'is-warn' : 'is-bad')
  return (
    <div className="wrap" style={{ paddingBottom: 64 }}>
      <section className="status-hero">
        <h1>{heading}</h1>
        {c && (
          <p className="muted" style={{ maxWidth: '38rem' }}>
            {downModels.length > 0
              ? `${downModels.length} ta model hozir javob bermayapti. Qolgan modellar odatdagidek ishlaydi; javob kelmagan so'rov uchun pul yechilmaydi.`
              : c.model_online
                ? `Barcha modellar so'rovlarga javob bermoqda${c.since ? `, ${fmtDuration(Date.now() / 1000 - c.since)}dan beri uzilishsiz` : ''}.`
                : `${c.label}. So'rovlar qabul qilinmaydi va pul yechilmaydi. Ulanish tiklanishi bilan avtomatik ishlay boshlaydi.`}
          </p>
        )}
        <div className="status-meta">
          <span className="date-pill">
            <IconClock />
            {updatedAt ? `Yangilandi: ${fmtTime(updatedAt)}` : 'Yangilanmoqda...'}
          </span>
          <span className="dim small">Har 30 soniyada o'zi yangilanadi.</span>
          <button className="btn btn--small" onClick={refresh} disabled={cur.loading}>
            {cur.loading ? 'Yangilanmoqda...' : 'Hozir yangilash'}
          </button>
        </div>
      </section>
      {models.length > 0 && (
        <section className="panel" style={{ marginTop: 8 }}>
          <div className="panel-head">
            <span className="ico-chip"><IconLayers /></span>
            <div className="panel-head__t" style={{ flex: 1 }}>
              <h2>Modellar holati</h2>
              <p>Har bir model alohida tekshiriladi.</p>
            </div>
          </div>
          <div className="model-state-list">
            {models.map((x) => (
              <div className="model-state" key={x.model}>
                <span className="model-state__name">
                  <b>{names[x.model] || x.model}</b>
                  <code className="dim small">{x.model}</code>
                </span>
                <span className="model-state__tag">
                  {x.state === 'down' && x.since && (
                    <span className="dim small">{fmtDuration(Date.now() / 1000 - x.since)}dan beri</span>
                  )}
                  <span className={`tag ${x.state === 'up' ? 'tag--ok' : x.state === 'down' ? 'tag--bad' : 'tag--warn'}`}>
                    <i className="req-dot" aria-hidden="true" />
                    {x.state === 'up' ? 'Ishlayapti' : x.state === 'down' ? 'Javob bermayapti' : 'Noma\'lum'}
                  </span>
                </span>
              </div>
            ))}
          </div>
        </section>
      )}

      {!h && <Loading rows={3} />}
      {h && (
        <>
          <div className="block-head" style={{ marginTop: 28 }}>
            <div className="panel-head__t">
              <h2>API xizmati ishonchliligi</h2>
              <p className="muted small">
                Xizmat har daqiqada avtomatik tekshiriladi.
                {h.since_date && <> Hisob xizmat ishga tushgan kundan — {fmtDay(h.since_date)} dan boshlanadi.</>}
              </p>
            </div>
          </div>
          <div className="metric-grid">
            <div className="metric"><span className="label">24 soat</span><b>{fmtPct(h.uptime.h24)}</b><span className="trend up">uptime</span></div>
            <div className="metric"><span className="label">7 kun</span><b>{fmtPct(h.uptime.d7)}</b><span className="trend">uptime</span></div>
            <div className="metric"><span className="label">30 kun</span><b>{fmtPct(h.uptime.d30)}</b><span className="trend">uptime</span></div>
          </div>
          <div className="panel" style={{ marginTop: 24 }}>
            <div className="panel-head"><h2>{h.daily.length < 30 ? 'Ishga tushgandan beri' : 'Oxirgi 30 kun'}</h2></div>
            <div className="daybars" role="img" aria-label="Kunlik ishlash foizi">
              {h.daily.map((d) => <span key={d.date} className={cls(d)} title={`${fmtDay(d.date)}: ${d.uptime == null ? 'ma\'lumot yo\'q' : fmtPct(d.uptime)}${d.incidents ? `, ${d.incidents} ta uzilish` : ''}`} />)}
            </div>
            <div className="legend">
              <span><i className="tl tl--up" /> 99,5% dan yuqori</span>
              <span><i className="tl" style={{ background: 'var(--amber)' }} /> 95-99,5%</span>
              <span><i className="tl tl--tunnel_down" /> 95% dan past</span>
              <span><i className="tl tl--none" /> Ma'lumot yo'q</span>
            </div>
          </div>
          <div className="panel" style={{ marginTop: 16 }}>
            <div className="panel-head"><h2>Uzilishlar</h2></div>
            {h.incidents.length === 0 ? <p className="muted">{h.daily.length < 30 ? 'Ishga tushgandan beri' : "So'nggi 30 kunda"} uzilish bo'lmagan.</p> : (
              <div className="table-wrap"><table className="table">
                <thead><tr><th>Boshlandi</th><th>Davomiyligi</th><th>Sabab</th></tr></thead>
                <tbody>{h.incidents.map((i) => (
                  <tr key={i.started_at}><td className="nowrap">{fmtDateTime(i.started_at)}</td><td className="nowrap"><b>{fmtDuration(i.duration)}</b>{i.ongoing && <> <span className="tag tag--bad">davom etmoqda</span></>}</td><td className="muted">{i.reason_text}</td></tr>
                ))}</tbody>
              </table></div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
