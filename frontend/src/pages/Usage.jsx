import { useState } from 'react'
import { api, qs } from '../api.js'
import BarChart from '../components/BarChart.jsx'
import FreeQuota from '../components/FreeQuota.jsx'
import ModelBreakdown from '../components/ModelBreakdown.jsx'
import { useAuth } from '../auth.js'
import { ErrorNote, Loading, PageHead, Pager, StatCard, useLoad } from '../components/ui.jsx'
import { IconChart, IconClock, IconCoins, IconLayers, IconSearch } from '../components/icons.jsx'
import { levelShort } from '../reasoning.jsx'
import { useT } from '../i18n/index.jsx'
import { fmtDateTime, fmtInt, fmtSom } from '../format.js'

const LIMIT = 25

export default function Usage() {
  const { freeMode } = useAuth()
  const t = useT()
  const [offset, setOffset] = useState(0)
  const [keyId, setKeyId] = useState('')
  const [failed, setFailed] = useState(false)
  const [q, setQ] = useState('')
  const keys = useLoad(() => api.get('/portal/keys'), [])
  const daily = useLoad(() => api.get('/portal/usage/daily?days=30'), [], { refreshMs: 15_000 })
  const sum = useLoad(() => api.get('/portal/account/summary'), [], { refreshMs: 15_000 })
  const { data, error, loading, reload } = useLoad(
    () => api.get('/portal/usage' + qs({ limit: LIMIT, offset, key_id: keyId, failed })),
    [offset, keyId, failed], { refreshMs: 15_000 })

  const days = daily.data?.items || []
  const monthCost = days.reduce((s, d) => s + (d.cost || 0), 0)
  const monthTokens = days.reduce((s, d) => s + (d.prompt_tokens || 0) + (d.completion_tokens || 0), 0)
  const monthIn = days.reduce((s, d) => s + (d.prompt_tokens || 0), 0)
  const monthOut = days.reduce((s, d) => s + (d.completion_tokens || 0), 0)

  const rows = (data?.items || []).filter((r) => {
    const needle = q.trim().toLowerCase()
    if (!needle) return true
    return [r.model, r.key_name, r.reason, r.error].filter(Boolean).some((t) => String(t).toLowerCase().includes(needle))
  })
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

      <div className="usage-top">
        <div className="usage-stats">
        <StatCard icon={<IconCoins />} label={t('usage.totalCost')} value={fmtSom(monthCost, { unit: false })} unit={t('common.som')} note={t('usage.last30')} />
        <StatCard icon={<IconLayers />} label={t('usage.totalTokens')} value={fmtInt(monthTokens)}
                  note={t('req.tokensInOut', { in: fmtInt(monthIn), out: fmtInt(monthOut) })} />
        <StatCard icon={<IconClock />} label={t('usage.avgLatency')} value={avgLat != null ? t('common.seconds', { n: avgLat.toFixed(1) }) : '—'} note={t('usage.onThisPage')} />
        </div>
        <section className="panel usage-chart">
          <div className="panel-head" style={{ marginBottom: 8 }}>
            <div className="panel-head__t"><h3><span className="ico-chip ico-chip--sm"><IconChart /></span> {t('usage.dailyCost')}</h3></div>
          </div>
          {days.length > 0 ? <BarChart items={days.slice(-7)} metric="cost" compact /> : <Loading rows={1} />}
        </section>
      </div>

      <FreeQuota quota={sum.data?.free_daily} style={{ marginTop: 18 }} />

      <section className="panel block">
        <div className="panel-head" style={{ marginBottom: 8 }}>
          <div className="panel-head__t"><h3><span className="ico-chip ico-chip--sm"><IconLayers /></span> {t('usage.byModel')}</h3></div>
        </div>
        <ModelBreakdown days={30} freeMode={freeMode} />
      </section>

      <div className="filter-bar block">
        <label className="search-field">
          <IconSearch />
          <input className="input" value={q} onChange={(e) => setQ(e.target.value)} placeholder={t('usage.searchPh')} aria-label={t('usage.searchLabel')} />
        </label>
        <select className="input" style={{ width: 'auto' }} value={keyId} onChange={(e) => { setKeyId(e.target.value); setOffset(0) }} aria-label={t('usage.keyFilter')}>
          <option value="">{t('usage.allKeys')}</option>
          {(keys.data?.items || []).map((k) => <option key={k.id} value={k.id}>{k.name}</option>)}
        </select>
        <label className="check"><input type="checkbox" checked={failed} onChange={(e) => { setFailed(e.target.checked); setOffset(0) }} /> {t('usage.onlyErrors')}</label>
      </div>

      {loading && !data && <Loading rows={5} />}
      {data && rows.length === 0 && (
        <div className="empty"><b>{t('usage.emptyTitle')}</b><span>{failed ? t('usage.emptyErrors') : t('usage.emptyText')}</span></div>
      )}
      {data && rows.length > 0 && (
        <section className="panel">
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>{t('usage.colTime')}</th><th>{t('usage.colModel')}</th><th>{t('usage.colTokens')}</th><th>{t('usage.colType')}</th>
                  <th>{t('usage.colLatency')}</th><th>{t('usage.colCost')}</th><th>{t('usage.colStatus')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const bad = r.status_code >= 400
                  return (
                    <tr key={r.id}>
                      <td className="muted nowrap">{fmtDateTime(r.created_at)}</td>
                      <td><span className="tag tag--sky tag--mono">{r.model}</span></td>
                      <td className="nowrap">
                        {bad ? <span className="muted">—</span> : (<>
                          <b>{fmtInt(r.prompt_tokens)}</b> <span className="dim">{t('usage.in')}</span> + <b>{fmtInt(r.completion_tokens)}</b> <span className="dim">{t('usage.out')}</span>
                          <span className="stack dim small">{t('chart.tokens', { n: fmtInt(r.prompt_tokens + r.completion_tokens) })}{r.estimated ? ` ${t('usage.estimated')}` : ''}</span>
                        </>)}
                      </td>
                      <td>{r.reasoning
                        ? <span className="tag tag--violet">{t('usage.reasoning', { level: levelShort(t, r.reasoning) })}</span>
                        : <span className="tag tag--ok">{t('usage.chat')}</span>}</td>
                      <td className="muted nowrap">{r.latency_ms ? t('common.seconds', { n: (r.latency_ms / 1000).toFixed(1) }) : '—'}</td>
                      <td className="nowrap">{r.cost === 0
                        ? <span className="tag">{t('common.free')}</span>
                        : <span className="tag tag--bad">{fmtSom(-r.cost, { sign: true })}</span>}</td>
                      <td className="nowrap">
                        <span className={`tag ${bad ? 'tag--bad' : 'tag--ok'}`}>
                          <i className="req-dot" aria-hidden="true" />{bad ? t('req.error', { code: r.status_code }) : t('req.success')}
                        </span>
                        {bad && <span className="stack dim small">{t('req.failedNote', { reason: r.reason || r.error || t('usage.failed') })}</span>}
                      </td>
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
