import { translateText as tr, useLang as useLocaleRefresh } from '../../i18n/index.jsx'
import { useEffect, useState } from 'react'
import { api, qs } from '../../api.js'
import Timeline from '../../components/Timeline.jsx'
import { Empty, ErrorNote, Loading, Pager, useLoad } from '../../components/ui.jsx'
import { fmtAgo, fmtDateTime, fmtDuration, fmtInt, fmtPct } from '../../format.js'

const LIMIT = 15

function ToolHealth() {
  const { data, error, reload } = useLoad(() => api.get('/portal/admin/cluster/metrics'), [], { refreshMs: 30_000 })
  const rows = data?.per_model || []
  return <section className="panel block">
    <div className="panel-head"><h2>{tr('Vosita protokoli')}</h2></div>
    <p className="hint">{tr('Klaster ishga tushganidan beri. Kesh javoblari model tezligidan alohida hisoblanadi.')}</p>
    <ErrorNote error={error} retry={reload} />
    {data && <div className="table-wrap"><table className="table">
      <thead><tr>{['Model', 'So‘rovlar', 'Qayta so‘rashlar', 'Chaqiruv tiklandi', 'Chaqiruv qaytmadi', 'Qayta so‘rov p95', 'Javob keshi'].map(label => <th key={label}>{tr(label)}</th>)}</tr></thead>
      <tbody>{rows.map(row => <tr key={row.model}>
        <td><code>{row.model}</code></td><td>{fmtInt(row.tool_protocol?.requests || 0)}</td>
        <td>{fmtInt(row.tool_protocol?.retries || 0)}</td><td>{fmtInt(row.tool_protocol?.recovered || 0)}</td>
        <td>{fmtInt(row.tool_protocol?.missing_calls || 0)}</td><td>{fmtInt(row.tool_protocol?.retry_ms_p95 || 0)} ms</td>
        <td>{fmtInt(row.cache_hits || 0)}</td>
      </tr>)}</tbody>
    </table></div>}
  </section>
}

function Failures({ days }) {
  useLocaleRefresh()
  const [category, setCategory] = useState('all')
  const [offset, setOffset] = useState(0)
  const { data, error, reload } = useLoad(() => api.get('/portal/admin/failures' + qs({ category, days, limit: LIMIT, offset })), [category, days, offset])
  useEffect(() => { setOffset(0) }, [category, days])
  const all = data ? data.counts.reduce((s, c) => s + c.count, 0) : 0
  return (
    <div className="block panel">
      <div className="panel-head">
        <h2>{tr("Bajarilmagan so'rovlar")}</h2>
        <span style={{ display: 'flex', gap: 8 }}>
          <a className="btn btn--small" href={`/portal/admin/reports/usage.csv?days=${days}&only_failed=true`}>{tr("CSV: xatolar")}</a>
          <a className="btn btn--small" href={`/portal/admin/reports/usage.csv?days=${days}`}>{tr("CSV: barcha so'rovlar")}</a>
        </span>
      </div>
      <div className="chips" role="group" aria-label={tr("Toifa")}>
        <button className={`chip ${category === 'all' ? 'is-on' : ''}`} onClick={() => setCategory('all')}>{tr("Hammasi")}<b>{tr(all)}</b></button>
        {data?.counts.map((c) => <button key={c.category} className={`chip ${category === c.category ? 'is-on' : ''}`} onClick={() => setCategory(c.category)}>{tr(c.label)}<b>{tr(c.count)}</b></button>)}
      </div>
      <ErrorNote error={error} retry={reload} />
      {!data && !error && <Loading rows={3} />}
      {data && data.items.length === 0 && <Empty title={tr("Bu davrda bajarilmagan so'rov yo'q")}>{tr("Tanlangan toifada xato topilmadi.")}</Empty>}
      {data && data.items.length > 0 && (
        <>
          <div className="table-wrap" style={{ marginTop: 10 }}><table className="table">
            <thead><tr><th>{tr("Vaqt")}</th><th>{tr("Foydalanuvchi")}</th><th>{tr("Sabab")}</th><th>HTTP</th><th>{tr("So'rov ID")}</th></tr></thead>
            <tbody>
              {data.items.map((f) => (
                <tr key={f.id}>
                  <td className="muted nowrap">{fmtDateTime(f.created_at)}</td>
                  <td>{tr(f.email || '-')}{f.key_name && <div className="dim small">{tr(f.key_name)}</div>}</td>
                  <td>{tr(f.reason)}<div className="dim small">{tr(f.category_label)}{tr(f.model !== '-' ? `, ${f.model}` : '')}</div></td>
                  <td><span className={`tag ${f.category === 'platform' ? 'tag--bad' : ''}`}>{tr(f.status_code)}</span></td>
                  <td><code className="dim">{f.request_id}</code></td>
                </tr>
              ))}
            </tbody>
          </table></div>
          <Pager total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </>
      )}
    </div>
  )
}

export default function Monitoring() {
  useLocaleRefresh()
  const [days, setDays] = useState(7)
  const { data: d, error, reload } = useLoad(() => api.get(`/portal/admin/monitoring?days=${days}`), [days], { refreshMs: 30_000 })
  useEffect(() => { const t = setInterval(reload, 20000); return () => clearInterval(t) }, [reload])
  const s = d?.status
  const q = d?.quality
  const maxCat = q ? Math.max(...q.by_category.map((c) => c.count), 1) : 1

  return (
    <>
      <div className="page-head">
        <div><h1>{tr("Monitoring")}</h1><p>{tr("Ishxonadagi kompyuter va model holati, uzilishlar hamda bajarilmagan so'rovlar. Sahifa 20 soniyada yangilanadi.")}</p></div>
        <div className="seg" role="group" aria-label={tr("Davr")}>
          {[[1, tr("24 soat")], [7, tr("7 kun")], [30, tr("30 kun")]].map(([n, l]) => <button key={n} className={days === n ? 'is-on' : ''} onClick={() => setDays(n)}>{tr(l)}</button>)}
        </div>
      </div>
      <ErrorNote error={error} retry={reload} />
      {!d && !error && <Loading rows={4} />}
      {d && (
        <>
          {!d.monitor_enabled && <div className="alert alert--warn" style={{ marginBottom: 18 }}>{tr("Fon monitoringi o'chirilgan (SF_MONITOR=0): tarix yozilmaydi.")}</div>}
          <div className="panel" style={{ marginBottom: 20 }}>
            <div className="big-state">
              <span className={`dot ${s.online ? 'dot--on' : 'dot--off'}`} />
              <b>{tr(s.label)}</b>
              {s.since && <span className="muted">{fmtDuration(Date.now() / 1000 - s.since)}{tr("dan beri")}</span>}
            </div>
            {s.error && !s.online && <p className="dim small" style={{ marginTop: 10 }}>{tr("Tekshiruv xabari:")} {tr(s.error)}</p>}
          </div>
          <div className="metric-grid">
            <div className="metric"><span className="label">{tr("24 soat")}</span><b>{fmtPct(d.uptime.h24)}</b><span className="trend up">{tr("uptime")}</span></div>
            <div className="metric"><span className="label">{tr("7 kun")}</span><b>{fmtPct(d.uptime.d7)}</b><span className="trend">{tr("uptime")}</span></div>
            <div className="metric"><span className="label">{tr("30 kun")}</span><b>{fmtPct(d.uptime.d30)}</b><span className="trend">{tr("uptime")}</span></div>
            <div className="metric"><span className="label">{tr("Tekshiruv")}</span><b>{tr(s.latency_ms != null ? `${s.latency_ms}` : '-')}<small>{tr("ms")}</small></b><span className="trend">{tr(s.last_check ? fmtAgo(s.last_check) : '-')}</span></div>
            <div className="metric"><span className="label">{tr("Hozirgi so'rovlar")}</span><b>{tr(s.in_flight)}<small> / {tr(s.queued)}</small></b><span className="trend">{tr("bajarilmoqda / navbatda")}</span></div>
          </div>

          {s.providers && <section className="panel block">
            <div className="panel-head"><h2>{tr("Taqsimot")}</h2><span className="dim small">{tr("Faol / navbatda")}</span></div>
            <div className="metric-grid">{Object.entries(s.providers).map(([provider, counts]) =>
              <div key={provider} className="metric"><span className="label">{tr(({ local: "Mahalliy modellar", cluster: "Brauzer klasteri", external: "Tashqi API" })[provider] || provider)}</span>
                <b>{counts.active}<small> / {counts.queued}</small></b></div>)}</div>
          </section>}
          <div className="block panel">
            <div className="panel-head"><h2>{tr("Oxirgi 24 soat")}</h2></div>
            <Timeline items={d.timeline} />
          </div>

          <div className="block panel">
            <div className="panel-head"><h2>{tr("Uzilishlar")}</h2></div>
            {d.incidents.length === 0
              ? <Empty title={tr("Uzilish bo'lmagan")}>{tr("Model serveri uzilmasdan ishlagan. Birinchi uzilish shu yerda yoziladi.")}</Empty>
              : (
                <div className="table-wrap"><table className="table">
                  <thead><tr><th>{tr("Boshlandi")}</th><th>{tr("Tugadi")}</th><th>{tr("Davomiyligi")}</th><th>{tr("Sabab")}</th><th className="num">{tr("Bajarilmagan so'rov")}</th><th className="num">{tr("Foydalanuvchi")}</th></tr></thead>
                  <tbody>
                    {d.incidents.map((i) => (
                      <tr key={i.id}>
                        <td className="nowrap">{fmtDateTime(i.started_at)}</td>
                        <td className="nowrap">{tr(i.ongoing ? <span className="tag tag--bad">{tr("davom etmoqda")}</span> : fmtDateTime(i.ended_at))}</td>
                        <td className="nowrap"><b>{fmtDuration(i.duration)}</b></td>
                        <td>{tr(i.reason_text)}</td>
                        <td className="num"><b>{fmtInt(i.failed_requests)}</b></td>
                        <td className="num muted">{fmtInt(i.affected_users)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table></div>
              )}
          </div>

          <div className="cols block">
            <section className="panel">
              <div className="panel-head"><h2>{tr("So'rovlar sifati")}</h2></div>
              <div className="metric-grid">
                <div className="metric"><span className="label">{tr("So'rovlar")}</span><b>{fmtInt(q.requests)}</b></div>
                <div className="metric"><span className="label">{tr("Muvaffaqiyat")}</span><b>{fmtPct(q.success_rate)}</b></div>
                <div className="metric"><span className="label">{tr("Ishonchlilik")}</span><b>{fmtPct(q.reliability)}</b></div>
                <div className="metric"><span className="label">{tr("Javob (o'rta)")}</span><b>{tr(q.latency_p50 != null ? `${(q.latency_p50 / 1000).toFixed(1)}` : '-')}<small>{tr("s")}</small></b></div>
                <div className="metric"><span className="label">{tr("Javob (95%)")}</span><b>{tr(q.latency_p95 != null ? `${(q.latency_p95 / 1000).toFixed(1)}` : '-')}<small>{tr("s")}</small></b></div>
                <div className="metric"><span className="label">TTFB</span><b>{tr(q.ttfb_p50 != null ? `${(q.ttfb_p50 / 1000).toFixed(1)}` : '-')}<small>{tr("s")}</small></b></div>
              </div>
              <p className="hint" style={{ marginTop: 14 }}>{tr("\"Ishonchlilik\": faqat model yoki serverga bog'liq xatolar. Hisob yetmaganlik, chegara va noto'g'ri so'rovlar kirmaydi.")}</p>
            </section>
            <section className="panel">
              <div className="panel-head"><h2>{tr("Xatolar toifalari")}</h2></div>
              {tr(q.failed === 0 ? <p className="muted">{tr("Bu davrda xato bo'lmagan.")}</p> : q.by_category.map((c) => (
                <div className="hbar" key={c.category}>
                  <span>{tr(c.label)}</span>
                  <span className="track"><span className={`fill ${c.category === 'platform' ? 'fill--bad' : ''}`} style={{ width: `${(c.count / maxCat) * 100}%` }} /></span>
                  <b className="num">{fmtInt(c.count)}</b>
                </div>
              )))}
            </section>
          </div>

          <p className="hint">{tr('Model tezligi: ma’lum kesh javoblari chiqarib tashlangan. Eski yozuvlarda kesh belgisi bo‘lmasligi mumkin.')}</p>
          <ToolHealth />

          <Failures days={days} />
        </>
      )}
    </>
  )
}
