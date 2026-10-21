import { useEffect, useState } from 'react'
import { api, qs } from '../../api.js'
import Timeline from '../../components/Timeline.jsx'
import { Empty, ErrorNote, Loading, Pager, useLoad } from '../../components/ui.jsx'
import { fmtAgo, fmtDateTime, fmtDuration, fmtInt, fmtPct } from '../../format.js'

const LIMIT = 15

function Failures({ days }) {
  const [category, setCategory] = useState('all')
  const [offset, setOffset] = useState(0)
  const { data, error, reload } = useLoad(() => api.get('/portal/admin/failures' + qs({ category, days, limit: LIMIT, offset })), [category, days, offset])
  useEffect(() => { setOffset(0) }, [category, days])
  const all = data ? data.counts.reduce((s, c) => s + c.count, 0) : 0
  return (
    <div className="block panel">
      <div className="panel-head">
        <h2>Bajarilmagan so'rovlar</h2>
        <span style={{ display: 'flex', gap: 8 }}>
          <a className="btn btn--small" href={`/portal/admin/reports/usage.csv?days=${days}&only_failed=true`}>CSV: xatolar</a>
          <a className="btn btn--small" href={`/portal/admin/reports/usage.csv?days=${days}`}>CSV: barcha so'rovlar</a>
        </span>
      </div>
      <div className="chips" role="group" aria-label="Toifa">
        <button className={`chip ${category === 'all' ? 'is-on' : ''}`} onClick={() => setCategory('all')}>Hammasi<b>{all}</b></button>
        {data?.counts.map((c) => <button key={c.category} className={`chip ${category === c.category ? 'is-on' : ''}`} onClick={() => setCategory(c.category)}>{c.label}<b>{c.count}</b></button>)}
      </div>
      <ErrorNote error={error} retry={reload} />
      {!data && !error && <Loading rows={3} />}
      {data && data.items.length === 0 && <Empty title="Bu davrda bajarilmagan so'rov yo'q">Tanlangan toifada xato topilmadi.</Empty>}
      {data && data.items.length > 0 && (
        <>
          <div className="table-wrap" style={{ marginTop: 10 }}><table className="table">
            <thead><tr><th>Vaqt</th><th>Foydalanuvchi</th><th>Sabab</th><th>HTTP</th><th>So'rov ID</th></tr></thead>
            <tbody>
              {data.items.map((f) => (
                <tr key={f.id}>
                  <td className="muted nowrap">{fmtDateTime(f.created_at)}</td>
                  <td>{f.email || '-'}{f.key_name && <div className="dim small">{f.key_name}</div>}</td>
                  <td>{f.reason}<div className="dim small">{f.category_label}{f.model !== '-' ? `, ${f.model}` : ''}</div></td>
                  <td><span className={`tag ${f.category === 'platform' ? 'tag--bad' : ''}`}>{f.status_code}</span></td>
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
  const [days, setDays] = useState(7)
  const { data: d, error, reload } = useLoad(() => api.get(`/portal/admin/monitoring?days=${days}`), [days], { refreshMs: 30_000 })
  useEffect(() => { const t = setInterval(reload, 20000); return () => clearInterval(t) }, [reload])
  const s = d?.status
  const q = d?.quality
  const maxCat = q ? Math.max(...q.by_category.map((c) => c.count), 1) : 1

  return (
    <>
      <div className="page-head">
        <div><h1>Monitoring</h1><p>Ishxonadagi kompyuter va model holati, uzilishlar hamda bajarilmagan so'rovlar. Sahifa 20 soniyada yangilanadi.</p></div>
        <div className="seg" role="group" aria-label="Davr">
          {[[1, '24 soat'], [7, '7 kun'], [30, '30 kun']].map(([n, l]) => <button key={n} className={days === n ? 'is-on' : ''} onClick={() => setDays(n)}>{l}</button>)}
        </div>
      </div>
      <ErrorNote error={error} retry={reload} />
      {!d && !error && <Loading rows={4} />}
      {d && (
        <>
          {!d.monitor_enabled && <div className="alert alert--warn" style={{ marginBottom: 18 }}>Fon monitoringi o'chirilgan (SF_MONITOR=0): tarix yozilmaydi.</div>}
          <div className="panel" style={{ marginBottom: 20 }}>
            <div className="big-state">
              <span className={`dot ${s.online ? 'dot--on' : 'dot--off'}`} />
              <b>{s.label}</b>
              {s.since && <span className="muted">{fmtDuration(Date.now() / 1000 - s.since)}dan beri</span>}
            </div>
            {s.error && !s.online && <p className="dim small" style={{ marginTop: 10 }}>Tekshiruv xabari: {s.error}</p>}
          </div>
          <div className="metric-grid">
            <div className="metric"><span className="label">24 soat</span><b>{fmtPct(d.uptime.h24)}</b><span className="trend up">uptime</span></div>
            <div className="metric"><span className="label">7 kun</span><b>{fmtPct(d.uptime.d7)}</b><span className="trend">uptime</span></div>
            <div className="metric"><span className="label">30 kun</span><b>{fmtPct(d.uptime.d30)}</b><span className="trend">uptime</span></div>
            <div className="metric"><span className="label">Tekshiruv</span><b>{s.latency_ms != null ? `${s.latency_ms}` : '-'}<small>ms</small></b><span className="trend">{s.last_check ? fmtAgo(s.last_check) : '-'}</span></div>
            <div className="metric"><span className="label">Hozirgi so'rovlar</span><b>{s.in_flight}<small> / {s.queued}</small></b><span className="trend">bajarilmoqda / navbatda</span></div>
          </div>

          <div className="block panel">
            <div className="panel-head"><h2>Oxirgi 24 soat</h2></div>
            <Timeline items={d.timeline} />
          </div>

          <div className="block panel">
            <div className="panel-head"><h2>Uzilishlar</h2></div>
            {d.incidents.length === 0
              ? <Empty title="Uzilish bo'lmagan">Model serveri uzilmasdan ishlagan. Birinchi uzilish shu yerda yoziladi.</Empty>
              : (
                <div className="table-wrap"><table className="table">
                  <thead><tr><th>Boshlandi</th><th>Tugadi</th><th>Davomiyligi</th><th>Sabab</th><th className="num">Bajarilmagan so'rov</th><th className="num">Foydalanuvchi</th></tr></thead>
                  <tbody>
                    {d.incidents.map((i) => (
                      <tr key={i.id}>
                        <td className="nowrap">{fmtDateTime(i.started_at)}</td>
                        <td className="nowrap">{i.ongoing ? <span className="tag tag--bad">davom etmoqda</span> : fmtDateTime(i.ended_at)}</td>
                        <td className="nowrap"><b>{fmtDuration(i.duration)}</b></td>
                        <td>{i.reason_text}</td>
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
              <div className="panel-head"><h2>So'rovlar sifati</h2></div>
              <div className="metric-grid">
                <div className="metric"><span className="label">So'rovlar</span><b>{fmtInt(q.requests)}</b></div>
                <div className="metric"><span className="label">Muvaffaqiyat</span><b>{fmtPct(q.success_rate)}</b></div>
                <div className="metric"><span className="label">Ishonchlilik</span><b>{fmtPct(q.reliability)}</b></div>
                <div className="metric"><span className="label">Javob (o'rta)</span><b>{q.latency_p50 != null ? `${(q.latency_p50 / 1000).toFixed(1)}` : '-'}<small>s</small></b></div>
                <div className="metric"><span className="label">Javob (95%)</span><b>{q.latency_p95 != null ? `${(q.latency_p95 / 1000).toFixed(1)}` : '-'}<small>s</small></b></div>
                <div className="metric"><span className="label">TTFB</span><b>{q.ttfb_p50 != null ? `${(q.ttfb_p50 / 1000).toFixed(1)}` : '-'}<small>s</small></b></div>
              </div>
              <p className="hint" style={{ marginTop: 14 }}>"Ishonchlilik": faqat model yoki serverga bog'liq xatolar. Hisob yetmaganlik, chegara va noto'g'ri so'rovlar kirmaydi.</p>
            </section>
            <section className="panel">
              <div className="panel-head"><h2>Xatolar toifalari</h2></div>
              {q.failed === 0 ? <p className="muted">Bu davrda xato bo'lmagan.</p> : q.by_category.map((c) => (
                <div className="hbar" key={c.category}>
                  <span>{c.label}</span>
                  <span className="track"><span className={`fill ${c.category === 'platform' ? 'fill--bad' : ''}`} style={{ width: `${(c.count / maxCat) * 100}%` }} /></span>
                  <b className="num">{fmtInt(c.count)}</b>
                </div>
              ))}
            </section>
          </div>

          <Failures days={days} />
        </>
      )}
    </>
  )
}
