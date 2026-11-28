import { translateText as tr } from '../i18n/index.jsx'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth, useSite } from '../auth.js'
import BarChart from '../components/BarChart.jsx'
import DonutShare from '../components/DonutShare.jsx'
import TokenTrend from '../components/TokenTrend.jsx'
import RequestList from '../components/RequestList.jsx'
import { useT } from '../i18n/index.jsx'
import { ErrorNote, Loading, PageHead, StatCard, useLoad } from '../components/ui.jsx'
import {
  IconArrowRight, IconCalendar, IconChart, IconClock, IconCoins, IconKey, IconLayers, IconPlus, IconWallet,
} from '../components/icons.jsx'
import { fmtDateLong, fmtInt, fmtSom, MICRO } from '../format.js'

/** Top models of the month as small cards: spend, requests, tokens, share. */
function ModelTiles({ items, freeMode }) {
  const t = useT()
  const tok = (m) => (m.prompt_tokens || 0) + (m.completion_tokens || 0)
  const total = items.reduce((s, m) => s + tok(m), 0) || 1
  return (
    <div className="model-tiles">
      {items.slice(0, 4).map((m) => (
        <div className="model-tile" key={m.model}>
          <div className="model-tile__head">
            <b title={m.model}>{tr(m.model)}</b>
            {!freeMode && <span>{fmtSom(m.cost)}</span>}
          </div>
          <dl>
            <div><dt>{t('breakdown.requests')}</dt><dd>{fmtInt(m.requests)}</dd></div>
            <div><dt>{t('breakdown.tokens')}</dt><dd>{fmtInt(tok(m))}</dd></div>
          </dl>
          <span className="model-tile__bar" title={`${Math.round((tok(m) / total) * 100)}%`}><i style={{ width: `${Math.max(2, (tok(m) / total) * 100)}%` }} /></span>
        </div>
      ))}
    </div>
  )
}

const TABS = [
  ['requests', 'dash.tabRequests'],
  ['cost', 'dash.tabCost'],
  ['tokens', 'dash.tabTokens'],
]

export default function Dashboard() {
  const { user, freeMode } = useAuth()
  const site = useSite()
  const t = useT()
  const sum = useLoad(() => api.get('/portal/account/summary'), [], { refreshMs: 15_000 })
  const daily = useLoad(() => api.get('/portal/usage/daily?days=30'), [], { refreshMs: 15_000 })
  const usage = useLoad(() => api.get('/portal/usage?limit=5'), [], { refreshMs: 15_000 })
  const keys = useLoad(() => api.get('/portal/keys'), [])
  const model = useLoad(() => api.get('/portal/status/models'), [], { refreshMs: 30_000 })
  const byModel = useLoad(() => api.get('/portal/usage/models?days=30'), [], { refreshMs: 15_000 })
  const [tab, setTab] = useState('requests')
  const s = sum.data
  const low = s && s.balance - s.held < 1000 * MICRO
  const models = byModel.data?.items || []
  const recent = (usage.data?.items || []).filter((r) => r.latency_ms)
  const avgLat = recent.length ? recent.reduce((a, r) => a + r.latency_ms, 0) / recent.length / 1000 : null

  // The name is split in two: the last part is shown in accent color (as in the mockup).
  const who = (user.name || user.email || '').trim()
  const sp = who.lastIndexOf(' ')
  const head = sp > 0 ? who.slice(0, sp + 1) : who
  const accent = sp > 0 ? who.slice(sp + 1) : null

  return (
    <>
      <PageHead
        crumbs={<nav className="crumbs" aria-label={t('common.breadcrumbs')}><Link to="/">{t('nav.home')}</Link><i>/</i><span>{t('shell.overview')}</span></nav>}
        title={[<span key="h">{t('dash.hello', { name: head })}</span>, accent ? `${accent}!` : null]}
        lead={t('dash.lead', { brand: site.brand_name })}
        action={<>
          <span className="date-pill"><IconCalendar /> {fmtDateLong()}</span>
          {!freeMode && <Link className="btn btn--primary" to="/app/billing"><IconPlus /> {t('dash.topUp')}</Link>}
        </>}
      />

      {model.data?.items?.some((item) => item.state === 'down') ? (
        <div className="alert alert--warn" style={{ marginBottom: 20 }}><span>{t('dash.someModelsOffline')} <Link to="/status">{t('dash.statusPage')}</Link></span></div>
      ) : model.data?.items?.some((item) => item.state === 'fallback') && (
        <div className="alert" style={{ marginBottom: 20 }}><span>{tr("Ayrim modellar zaxira rejimida ishlayapti: javoblar keladi, lekin biroz sekinroq bo'lishi mumkin.")} <Link to="/status">{t('dash.statusPage')}</Link></span></div>
      )}
      <ErrorNote error={sum.error} retry={sum.reload} />
      {!s && !sum.error && <Loading />}
      {s && (
        <>
          <div className="stat-grid stat-grid--3">
            {freeMode
              ? <StatCard tone="accent" icon={<IconWallet />} label={t('dash.usageLabel')} value={t('common.free')} note={t('dash.freeNote')} />
              : <StatCard icon={<IconWallet />} label={t('dash.balance')} value={fmtSom(s.balance, { unit: false })} unit={t('common.som')}
                          note={s.held > 0 ? `${t('dash.held')}: ${fmtSom(s.held)}` : t('dash.balanceNote')} />}
            <StatCard tone="sky" icon={<IconKey />} label={t('dash.activeKeys')} value={fmtInt(s.active_keys)}
                      note={<Link to="/app/keys">{t('shell.keys')}</Link>} />
            <StatCard tone="violet" icon={<IconChart />} label={tr('Bugungi so‘rovlar')} value={fmtInt(s.today.requests)}
                      note={`${tr('Shu oy')}: ${fmtInt(s.month.requests)}`} />
            {!freeMode && <StatCard tone="amber" icon={<IconCoins />} label={tr('Bugungi sarf')} value={fmtSom(s.today.cost, { unit: false })} unit={t('common.som')}
                      note={`${t('dash.spentMonth')}: ${fmtSom(s.month.cost)}`} />}
            <StatCard tone="rose" icon={<IconLayers />} label={t('dash.tokens')} value={fmtInt(s.month.tokens)} note={tr('shu oy')} />
            <StatCard tone="sky" icon={<IconClock />} label={t('usage.avgLatency')}
                      value={avgLat != null ? t('common.seconds', { n: avgLat.toFixed(1) }) : '—'} note={tr('so‘nggi so‘rovlar bo‘yicha')} />
          </div>
          {low && !freeMode && <div className="alert alert--warn" style={{ marginTop: 18 }}><span>{t('dash.lowBalance', { link: <Link to="/app/billing">{t('dash.lowBalanceLink')}</Link> })}</span></div>}
        </>
      )}

      {keys.data && keys.data.items.filter((k) => k.status === 'active').length === 0 && (
        <div className="alert alert--ok" style={{ marginTop: 20 }}>
          {t('dash.noKeys', {
            create: <Link to="/app/keys">{t('dash.noKeysCreate')}</Link>,
            docs: <Link to="/app/docs">{t('dash.noKeysDocs')}</Link>,
          })}
        </div>
      )}

      {models.length > 0 && (
        <section className="panel block">
          <div className="panel-head">
            <div className="panel-head__t"><h2>{t('dash.byModel')}</h2><p>{t('dash.byModelLead')}</p></div>
            <Link to="/app/usage" className="btn btn--link btn--small">{t('dash.all')} <IconArrowRight /></Link>
          </div>
          <ModelTiles items={models} freeMode={freeMode} />
        </section>
      )}

      {daily.data && models.length > 0 && (
        <div className="dash-charts block">
          <DonutShare title={tr('Modellar ulushi')} hint={tr('so‘nggi {n} kun', { n: 30 })}
                      rows={models.map((m) => ({ ...m, label: m.model }))} />
          <TokenTrend items={daily.data.items} />
        </div>
      )}

      <div className="cols block">
        <section className="panel">
          <div className="panel-head">
            <div className="panel-head__t">
              <h2>{t('dash.last30')}</h2>
              <p>{t('dash.last30Lead')}</p>
            </div>
            <div className="seg" role="tablist">
              {TABS.filter(([k]) => !(freeMode && k === 'cost')).map(([k, lbl]) => (
                <button key={k} role="tab" aria-selected={tab === k} className={tab === k ? 'is-on' : ''} onClick={() => setTab(k)}>{t(lbl)}</button>
              ))}
            </div>
          </div>
          {daily.data ? <BarChart items={daily.data.items} metric={tab} /> : <Loading rows={2} />}
        </section>
        <section className="panel">
          <div className="panel-head">
            <div className="panel-head__t">
              <h2>{t('dash.recent')}</h2>
              <p>{t('dash.recentLead')}</p>
            </div>
            <Link to="/app/usage" className="btn btn--link btn--small">{t('dash.all')} <IconArrowRight /></Link>
          </div>
          {usage.data
            ? <RequestList items={usage.data.items} empty={t('dash.recentEmpty')} />
            : <Loading rows={4} />}
        </section>
      </div>
    </>
  )
}
