import RequestAttribution from '../components/RequestAttribution.jsx'
import UsageAnalytics from '../components/UsageAnalytics.jsx'
import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { useEffect, useState } from 'react'
import { api, qs } from '../api.js'
import BarChart from '../components/BarChart.jsx'
import DonutShare from '../components/DonutShare.jsx'
import FreeQuota from '../components/FreeQuota.jsx'
import TokenTrend from '../components/TokenTrend.jsx'
import { ErrorNote, Loading, PageHead, Pager, StatCard, useLoad } from '../components/ui.jsx'
import { IconCalendar, IconChart, IconClock, IconCoins, IconLayers, IconSearch } from '../components/icons.jsx'
import { levelShort } from '../reasoning.jsx'
import { useT } from '../i18n/index.jsx'
import { fmtDateTime, fmtInt, fmtSom } from '../format.js'

const LIMIT = 25
const RANGES = [7, 30, 90]

/** Days folded into 7-day buckets, counted back from today; the bucket keeps its first date. */
function byWeek(days) {
  const out = []
  for (let end = days.length; end > 0; end -= 7) {
    const chunk = days.slice(Math.max(0, end - 7), end)
    out.unshift(chunk.reduce((o, d) => ({
      date: o.date, requests: o.requests + (d.requests || 0), cost: o.cost + (d.cost || 0),
      prompt_tokens: o.prompt_tokens + (d.prompt_tokens || 0), completion_tokens: o.completion_tokens + (d.completion_tokens || 0),
    }), { date: chunk[0].date, requests: 0, cost: 0, prompt_tokens: 0, completion_tokens: 0 }))
  }
  return out
}

export default function Usage() {
  useLocaleRefresh()
  const t = useT()
  const [offset, setOffset] = useState(0)
  const [keyId, setKeyId] = useState('')
  const [failed, setFailed] = useState(false)
  const [q, setQ] = useState('')
  const [query, setQuery] = useState('')
  const [range, setRange] = useState(30)
  const [grain, setGrain] = useState('day')
  useEffect(() => { const id = setTimeout(() => { setQuery(q); setOffset(0) }, 300); return () => clearTimeout(id) }, [q])
  const keys = useLoad(() => api.get('/portal/keys'), [])
  const daily = useLoad(() => api.get('/portal/usage/daily' + qs({ days: range, key_id: keyId })), [keyId, range], { refreshMs: 15_000 })
  const byModel = useLoad(() => api.get('/portal/usage/models' + qs({ days: range, key_id: keyId })), [keyId, range], { refreshMs: 15_000 })
  const sum = useLoad(() => api.get('/portal/account/summary'), [], { refreshMs: 15_000 })
  const { data, error, loading, reload } = useLoad(
    () => api.get('/portal/usage' + qs({ limit: LIMIT, offset, key_id: keyId, failed, q: query })),
    [offset, keyId, failed, query], { refreshMs: 15_000 })

  const days = daily.data?.items || []
  const monthCost = days.reduce((s, d) => s + (d.cost || 0), 0)
  const monthTokens = days.reduce((s, d) => s + (d.prompt_tokens || 0) + (d.completion_tokens || 0), 0)
  const monthIn = days.reduce((s, d) => s + (d.prompt_tokens || 0), 0)
  const monthOut = days.reduce((s, d) => s + (d.completion_tokens || 0), 0)
  const monthReq = days.reduce((s, d) => s + (d.requests || 0), 0)
  const series = grain === 'week' ? byWeek(days) : days
  const rangeNote = tr('so‘nggi {n} kun', { n: range })

  const rows = data?.items || []
  const lat = rows.filter((r) => r.latency_ms).map((r) => r.latency_ms)
  const avgLat = lat.length ? lat.reduce((a, b) => a + b, 0) / lat.length / 1000 : null

  return (
    <>
      <PageHead
        eyebrow={t('usage.eyebrow')}
        title={[t('usage.title1'), t('usage.title2')]}
        lead={t('usage.lead')}
      />
      <ErrorNote error={error} retry={reload} />

      <div className="toolbar">
        <div className="toolbar__field">
          <IconCalendar /> <span>{tr('Vaqt oralig‘i')}</span>
          <div className="seg" role="group" aria-label={tr('Vaqt oralig‘i')}>
            {RANGES.map((n) => <button key={n} type="button" aria-pressed={range === n} className={range === n ? 'is-on' : ''} onClick={() => setRange(n)}>{tr('{n} kun', { n })}</button>)}
          </div>
        </div>
        <div className="toolbar__field">
          <span>{tr('Bo‘linish')}</span>
          <div className="seg" role="group" aria-label={tr('Bo‘linish')}>
            {[['day', 'Kun'], ['week', 'Hafta']].map(([v, l]) => <button key={v} type="button" aria-pressed={grain === v} className={grain === v ? 'is-on' : ''} onClick={() => setGrain(v)}>{tr(l)}</button>)}
          </div>
        </div>
        <div className="toolbar__field toolbar__push">
          <select className="input" value={keyId} onChange={(e) => { setKeyId(e.target.value); setOffset(0) }} aria-label={t('usage.keyFilter')}>
            <option value="">{t('usage.allKeys')}</option>
            {(keys.data?.items || []).map((k) => <option key={k.id} value={k.id}>{tr(k.name)}</option>)}
          </select>
        </div>
      </div>

      <div className="stat-grid block">
        <StatCard tone="amber" icon={<IconCoins />} label={t('usage.totalCost')} value={fmtSom(monthCost, { unit: false })} unit={t('common.som')} note={rangeNote} />
        <StatCard tone="violet" icon={<IconChart />} label={tr('So‘rovlar')} value={fmtInt(monthReq)} note={rangeNote} />
        <StatCard tone="rose" icon={<IconLayers />} label={t('usage.totalTokens')} value={fmtInt(monthTokens)}
                  note={t('req.tokensInOut', { in: fmtInt(monthIn), out: fmtInt(monthOut) })} />
        <StatCard tone="sky" icon={<IconClock />} label={t('usage.avgLatency')} value={avgLat != null ? t('common.seconds', { n: avgLat.toFixed(1) }) : '—'} note={t('usage.onThisPage')} />
      </div>

      <FreeQuota quota={sum.data?.free_daily} style={{ marginTop: 18 }} />

      {byModel.data
        ? <div className="block"><DonutShare title={t('dash.byModel')} hint={rangeNote}
                      rows={(byModel.data.items || []).map((m) => ({ ...m, label: m.model }))} /></div>
        : <div className="block"><Loading rows={3} /></div>}

      {daily.data && (
        <div className="dash-charts dash-charts--even block">
          <TokenTrend items={series} />
          <section className="panel">
            <div className="panel-head" style={{ marginBottom: 8 }}>
              <div className="panel-head__t"><h3>{tr(grain === 'week' ? 'Haftalik xarajatlar' : t('usage.dailyCost'))}</h3><p>{rangeNote}</p></div>
            </div>
            {series.length > 0 ? <BarChart items={series} metric="cost" /> : <Loading rows={1} />}
          </section>
        </div>
      )}

      <UsageAnalytics keyId={keyId} onKeyChange={value => { setKeyId(value); setOffset(0) }} />

      <div className="filter-bar block">
        <label className="search-field">
          <IconSearch />
          <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t('usage.searchPh')} aria-label={t('usage.searchLabel')} />
        </label>
        <label className="check"><input type="checkbox" checked={failed} onChange={(e) => { setFailed(e.target.checked); setOffset(0) }} /> {t('usage.onlyErrors')}</label>
      </div>

      {loading && !data && <Loading rows={5} />}
      {data && rows.length === 0 && (
        <div className="empty"><b>{t('usage.emptyTitle')}</b><span>{tr(failed ? t('usage.emptyErrors') : t('usage.emptyText'))}</span></div>
      )}
      {data && rows.length > 0 && (
        <section className="panel">
          <div className="table-wrap">
            <table className="table table--usage">
              <thead>
                <tr>
                  <th>{t('usage.colTime')}</th><th>{t('usage.colModel')}</th><th>{t('usage.colTokens')}</th><th>{t('usage.colType')}</th>
                  <th>{t('usage.colLatency')}</th><th>{t('usage.colCost')}</th><th>{t('usage.colStatus')}</th><th>{tr('Tafsilotlar')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const bad = r.status_code >= 400
                  return (
                    <tr key={r.id}>
                      <td className="muted nowrap">{fmtDateTime(r.created_at)}</td>
                      <td><code className="model-id">{tr(r.model)}</code></td>
                      <td className="nowrap">
                        {bad ? <span className="muted">—</span> : (<>
                          <b>{fmtInt(r.prompt_tokens)}</b> <span className="dim">{t('usage.in')}</span> + <b>{fmtInt(r.completion_tokens)}</b> <span className="dim">{t('usage.out')}</span>
                          <span className="stack dim small">{t('chart.tokens', { n: fmtInt(r.prompt_tokens + r.completion_tokens) })}{tr(r.estimated ? ` ${t('usage.estimated')}` : '')}</span>
                        </>)}
                      </td>
                      <td>{r.reasoning
                        ? <span className="tag tag--violet">{t('usage.reasoning', { level: levelShort(t, r.reasoning) })}</span>
                        : <span className="muted">{t('usage.chat')}</span>}</td>
                      <td className="muted nowrap">{tr(r.latency_ms ? t('common.seconds', { n: (r.latency_ms / 1000).toFixed(1) }) : '—')}</td>
                      <td className="nowrap">{r.cost === 0
                        ? <span className="dim">{t('common.free')}</span>
                        : <span className="num">{fmtSom(-r.cost, { sign: true })}</span>}</td>
                      <td className="nowrap">
                        <span className={bad ? 'tag tag--bad' : 'status-ok'}>
                          <i className="req-dot" aria-hidden="true" />{tr(bad ? t('req.error', { code: r.status_code }) : t('req.success'))}
                        </span>
                        {bad && <span className="stack dim small">{t('req.failedNote', { reason: (r.reason || r.error) ? tr(r.reason || r.error) : t('usage.failed') })}</span>}
                      </td>
                      <td><RequestAttribution item={r} /></td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
          <Pager total={data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </section>
      )}
    </>
  )
}
