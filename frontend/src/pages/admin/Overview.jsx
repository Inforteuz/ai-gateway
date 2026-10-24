import { translateText as tr, useLang as useLocaleRefresh } from '../../i18n/index.jsx'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../api.js'
import BarChart from '../../components/BarChart.jsx'
import { ErrorNote, Loading, Status, useLoad } from '../../components/ui.jsx'
import { fmtInt, fmtSom } from '../../format.js'

export default function Overview() {
  useLocaleRefresh()
  const [days, setDays] = useState(30)
  const [metric, setMetric] = useState('cost')
  const { data: d, error, reload } = useLoad(() => api.get(`/portal/admin/stats?days=${days}`), [days], { refreshMs: 30_000 })
  return (
    <>
      <div className="page-head">
        <div><h1>{tr("Statistika")}</h1>{d && <p><Status online={d.model_online} /></p>}</div>
        <div className="seg" role="group" aria-label={tr("Davr")}>
          {[7, 30, 90].map((n) => <button key={n} className={days === n ? 'is-on' : ''} onClick={() => setDays(n)}>{tr(n)} {tr("kun")}</button>)}
        </div>
      </div>
      <ErrorNote error={error} retry={reload} />
      {!d && !error && <Loading rows={4} />}
      {d && (
        <>
          <div className="metric-grid" style={{ marginBottom: 18 }}>
            <div className="metric"><span className="label">{tr("Jami yechildi")}</span><b>{fmtSom(d.totals.cost, { unit: false })}<small style={{ fontSize: '0.5em', marginLeft: 4, fontWeight: 500 }}>{tr("so'm")}</small></b></div>
            <div className="metric"><span className="label">{tr("So'rovlar")}</span><b>{fmtInt(d.totals.requests)}</b></div>
            <div className="metric"><span className="label">{tr("Tokenlar")}</span><b>{fmtInt(d.totals.prompt_tokens + d.totals.completion_tokens)}</b><span className="trend">{fmtInt(d.totals.prompt_tokens)}↑ / {fmtInt(d.totals.completion_tokens)}↓</span></div>
            <div className="metric"><span className="label">{tr("Xatolar")}</span><b className={d.totals.errors > 0 ? 'trend down' : ''} style={{ color: d.totals.errors > 0 ? 'var(--danger)' : 'inherit' }}>{fmtInt(d.totals.errors)}</b></div>
            <div className="metric"><span className="label">{tr("O'rtacha javob")}</span><b>{(d.totals.avg_latency_ms / 1000).toFixed(1)}<small style={{ fontSize: '0.5em', marginLeft: 4, fontWeight: 500 }}>{tr("s")}</small></b></div>
          </div>
          <div className="cols block">
            <section className="panel">
              <div className="panel-head">
                <h2>{tr("Kunlik dinamika")}</h2>
                <div className="seg"><button className={metric === 'cost' ? 'is-on' : ''} onClick={() => setMetric('cost')}>{tr("Pul")}</button><button className={metric === 'requests' ? 'is-on' : ''} onClick={() => setMetric('requests')}>{tr("So'rovlar")}</button></div>
              </div>
              <BarChart items={d.daily} metric={metric} />
            </section>
            <section className="panel">
              <div className="panel-head"><h2>{tr("Eng faol foydalanuvchilar")}</h2></div>
              {d.top_users.length === 0 ? <p className="muted">{tr("Bu davrda so'rov bo'lmagan.")}</p> : (
                <table className="table"><tbody>
                  {d.top_users.map((u) => <tr key={u.id}><td><b>{tr(u.name || u.email)}</b><div className="dim small">{tr(u.email)}</div></td><td className="num">{fmtInt(u.requests)} {tr("ta")}</td><td className="num"><b>{fmtSom(u.cost)}</b></td></tr>)}
                </tbody></table>
              )}
            </section>
          </div>
          <div className="block panel">
            <div className="panel-head"><h2>{tr("Hisoblar")}</h2></div>
            <div className="metric-grid">
              <div className="metric"><span className="label">{tr("Jami balans")}</span><b>{fmtSom(d.balances.total, { unit: false })}<small>{tr("so'm")}</small></b></div>
              <div className="metric"><span className="label">{tr("Shu oy to'ldirildi")}</span><b>{fmtSom(d.balances.month_topups, { unit: false })}<small>{tr("so'm")}</small></b><span className="trend up">{tr("+ kirim")}</span></div>
              <div className="metric"><span className="label">{tr("Shu oy yechildi")}</span><b>{fmtSom(d.balances.month_cost, { unit: false })}<small>{tr("so'm")}</small></b></div>
              <div className="metric"><span className="label">{tr("Foydalanuvchilar")}</span><b>{fmtInt(d.users.active_period)}<small> / {fmtInt(d.users.total)}</small></b><span className="trend">{tr("faol / jami")}</span></div>
              {d.pending_topups > 0 && <div className="metric"><span className="label">{tr("Kutilayotgan")}</span><b><Link to="/app/admin/topups">{tr(d.pending_topups)}</Link></b><span className="trend">{tr("to'ldirish so'rovi")}</span></div>}
            </div>
          </div>
        </>
      )}
    </>
  )
}
