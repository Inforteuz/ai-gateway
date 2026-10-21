import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../../api.js'
import BarChart from '../../components/BarChart.jsx'
import { ErrorNote, Loading, Status, useLoad } from '../../components/ui.jsx'
import { fmtInt, fmtSom } from '../../format.js'

export default function Overview() {
  const [days, setDays] = useState(30)
  const [metric, setMetric] = useState('cost')
  const { data: d, error, reload } = useLoad(() => api.get(`/portal/admin/stats?days=${days}`), [days], { refreshMs: 30_000 })
  return (
    <>
      <div className="page-head">
        <div><h1>Statistika</h1>{d && <p><Status online={d.model_online} /></p>}</div>
        <div className="seg" role="group" aria-label="Davr">
          {[7, 30, 90].map((n) => <button key={n} className={days === n ? 'is-on' : ''} onClick={() => setDays(n)}>{n} kun</button>)}
        </div>
      </div>
      <ErrorNote error={error} retry={reload} />
      {!d && !error && <Loading rows={4} />}
      {d && (
        <>
          <div className="metric-grid" style={{ marginBottom: 18 }}>
            <div className="metric"><span className="label">Jami yechildi</span><b>{fmtSom(d.totals.cost, { unit: false })}<small style={{ fontSize: '0.5em', marginLeft: 4, fontWeight: 500 }}>so'm</small></b></div>
            <div className="metric"><span className="label">So'rovlar</span><b>{fmtInt(d.totals.requests)}</b></div>
            <div className="metric"><span className="label">Tokenlar</span><b>{fmtInt(d.totals.prompt_tokens + d.totals.completion_tokens)}</b><span className="trend">{fmtInt(d.totals.prompt_tokens)}↑ / {fmtInt(d.totals.completion_tokens)}↓</span></div>
            <div className="metric"><span className="label">Xatolar</span><b className={d.totals.errors > 0 ? 'trend down' : ''} style={{ color: d.totals.errors > 0 ? 'var(--danger)' : 'inherit' }}>{fmtInt(d.totals.errors)}</b></div>
            <div className="metric"><span className="label">O'rtacha javob</span><b>{(d.totals.avg_latency_ms / 1000).toFixed(1)}<small style={{ fontSize: '0.5em', marginLeft: 4, fontWeight: 500 }}>s</small></b></div>
          </div>
          <div className="cols block">
            <section className="panel">
              <div className="panel-head">
                <h2>Kunlik dinamika</h2>
                <div className="seg"><button className={metric === 'cost' ? 'is-on' : ''} onClick={() => setMetric('cost')}>Pul</button><button className={metric === 'requests' ? 'is-on' : ''} onClick={() => setMetric('requests')}>So'rovlar</button></div>
              </div>
              <BarChart items={d.daily} metric={metric} />
            </section>
            <section className="panel">
              <div className="panel-head"><h2>Eng faol foydalanuvchilar</h2></div>
              {d.top_users.length === 0 ? <p className="muted">Bu davrda so'rov bo'lmagan.</p> : (
                <table className="table"><tbody>
                  {d.top_users.map((u) => <tr key={u.id}><td><b>{u.name || u.email}</b><div className="dim small">{u.email}</div></td><td className="num">{fmtInt(u.requests)} ta</td><td className="num"><b>{fmtSom(u.cost)}</b></td></tr>)}
                </tbody></table>
              )}
            </section>
          </div>
          <div className="block panel">
            <div className="panel-head"><h2>Hisoblar</h2></div>
            <div className="metric-grid">
              <div className="metric"><span className="label">Jami balans</span><b>{fmtSom(d.balances.total, { unit: false })}<small>so'm</small></b></div>
              <div className="metric"><span className="label">Shu oy to'ldirildi</span><b>{fmtSom(d.balances.month_topups, { unit: false })}<small>so'm</small></b><span className="trend up">+ kirim</span></div>
              <div className="metric"><span className="label">Shu oy yechildi</span><b>{fmtSom(d.balances.month_cost, { unit: false })}<small>so'm</small></b></div>
              <div className="metric"><span className="label">Foydalanuvchilar</span><b>{fmtInt(d.users.active_period)}<small> / {fmtInt(d.users.total)}</small></b><span className="trend">faol / jami</span></div>
              {d.pending_topups > 0 && <div className="metric"><span className="label">Kutilayotgan</span><b><Link to="/app/admin/topups">{d.pending_topups}</Link></b><span className="trend">to'ldirish so'rovi</span></div>}
            </div>
          </div>
        </>
      )}
    </>
  )
}
