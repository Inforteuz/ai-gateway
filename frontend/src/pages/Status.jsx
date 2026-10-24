import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
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
  useLocaleRefresh()
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
    ? tr("Tekshirilmoqda...")
    : downModels.length > 0
      ? tr("Ba'zi modellar ishlamayapti")
      : c.model_online ? tr("Hamma narsa ishlayapti") : tr("Model hozir ulanmagan")

  const cls = (d) => (d.uptime == null ? '' : d.uptime >= 99.5 ? 'is-ok' : d.uptime >= 95 ? 'is-warn' : 'is-bad')
  return (
    <div className="wrap" style={{ paddingBottom: 64 }}>
      <section className="status-hero">
        <h1>{tr(heading)}</h1>
        {c && (
          <p className="muted" style={{ maxWidth: '38rem' }}>
            {tr(downModels.length > 0
              ? tr("{v0} ta model hozir javob bermayapti. Qolgan modellar odatdagidek ishlaydi; javob kelmagan so'rov uchun pul yechilmaydi.", { v0: (downModels.length) })
              : c.model_online
                ? tr("Barcha modellar so'rovlarga javob bermoqda{v0}.", { v0: (c.since ? `, ${fmtDuration(Date.now() / 1000 - c.since)}dan beri uzilishsiz` : '') })
                : tr("{v0}. So'rovlar qabul qilinmaydi va pul yechilmaydi. Ulanish tiklanishi bilan avtomatik ishlay boshlaydi.", { v0: (c.label) }))}
          </p>
        )}
        <div className="status-meta">
          <span className="date-pill">
            <IconClock />
            {tr(updatedAt ? tr("Yangilandi: {v0}", { v0: (fmtTime(updatedAt)) }) : 'Yangilanmoqda...')}
          </span>
          <span className="dim small">{tr("Har 30 soniyada o'zi yangilanadi.")}</span>
          <button className="btn btn--small" onClick={refresh} disabled={cur.loading}>
            {tr(cur.loading ? 'Yangilanmoqda...' : tr("Hozir yangilash"))}
          </button>
        </div>
      </section>
      {models.length > 0 && (
        <section className="panel" style={{ marginTop: 8 }}>
          <div className="panel-head">
            <span className="ico-chip"><IconLayers /></span>
            <div className="panel-head__t" style={{ flex: 1 }}>
              <h2>{tr("Modellar holati")}</h2>
              <p>{tr("Har bir model alohida tekshiriladi.")}</p>
            </div>
          </div>
          <div className="model-state-list">
            {models.map((x) => (
              <div className="model-state" key={x.model}>
                <span className="model-state__name">
                  <b>{tr(names[x.model] || x.model)}</b>
                  <code className="dim small">{x.model}</code>
                </span>
                <span className="model-state__tag">
                  {x.state === 'down' && x.since && (
                    <span className="dim small">{fmtDuration(Date.now() / 1000 - x.since)}{tr("dan beri")}</span>
                  )}
                  <span className={`tag ${x.state === 'up' ? 'tag--ok' : x.state === 'down' ? 'tag--bad' : 'tag--warn'}`}>
                    <i className="req-dot" aria-hidden="true" />
                    {tr(x.state === 'up' ? tr("Ishlayapti") : x.state === 'down' ? tr("Javob bermayapti") : tr("Noma'lum"))}
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
              <h2>{tr("API xizmati ishonchliligi")}</h2>
              <p className="muted small"> {tr("Xizmat har daqiqada avtomatik tekshiriladi.")} {h.since_date && <> {tr("Hisob xizmat ishga tushgan kundan —")} {fmtDay(h.since_date)} {tr("dan boshlanadi.")}</>}
              </p>
            </div>
          </div>
          <div className="metric-grid">
            <div className="metric"><span className="label">{tr("24 soat")}</span><b>{fmtPct(h.uptime.h24)}</b><span className="trend up">{tr("uptime")}</span></div>
            <div className="metric"><span className="label">{tr("7 kun")}</span><b>{fmtPct(h.uptime.d7)}</b><span className="trend">{tr("uptime")}</span></div>
            <div className="metric"><span className="label">{tr("30 kun")}</span><b>{fmtPct(h.uptime.d30)}</b><span className="trend">{tr("uptime")}</span></div>
          </div>
          <div className="panel" style={{ marginTop: 24 }}>
            <div className="panel-head"><h2>{tr(h.daily.length < 30 ? tr("Ishga tushgandan beri") : tr("Oxirgi 30 kun"))}</h2></div>
            <div className="daybars" role="img" aria-label={tr("Kunlik ishlash foizi")}>
              {h.daily.map((d) => <span key={d.date} className={cls(d)} title={`${fmtDay(d.date)}: ${d.uptime == null ? tr("ma'lumot yo'q") : fmtPct(d.uptime)}${d.incidents ? tr(", {v0} ta uzilish", { v0: (d.incidents) }) : ''}`} />)}
            </div>
            <div className="legend">
              <span><i className="tl tl--up" /> {tr("99,5% dan yuqori")}</span>
              <span><i className="tl" style={{ background: 'var(--amber)' }} /> 95-99,5%</span>
              <span><i className="tl tl--tunnel_down" /> {tr("95% dan past")}</span>
              <span><i className="tl tl--none" /> {tr("Ma'lumot yo'q")}</span>
            </div>
          </div>
          <div className="panel" style={{ marginTop: 16 }}>
            <div className="panel-head"><h2>{tr("Uzilishlar")}</h2></div>
            {h.incidents.length === 0 ? <p className="muted">{tr(h.daily.length < 30 ? tr("Ishga tushgandan beri") : tr("So'nggi 30 kunda"))} {tr("uzilish bo'lmagan.")}</p> : (
              <div className="table-wrap"><table className="table">
                <thead><tr><th>{tr("Boshlandi")}</th><th>{tr("Davomiyligi")}</th><th>{tr("Sabab")}</th></tr></thead>
                <tbody>{h.incidents.map((i) => (
                  <tr key={i.started_at}><td className="nowrap">{fmtDateTime(i.started_at)}</td><td className="nowrap"><b>{fmtDuration(i.duration)}</b>{i.ongoing && <> <span className="tag tag--bad">{tr("davom etmoqda")}</span></>}</td><td className="muted">{tr(i.reason_text)}</td></tr>
                ))}</tbody>
              </table></div>
            )}
          </div>
        </>
      )}
    </div>
  )
}
