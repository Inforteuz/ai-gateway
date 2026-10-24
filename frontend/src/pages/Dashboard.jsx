import { translateText as tr } from '../i18n/index.jsx'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth, useSite } from '../auth.js'
import BarChart from '../components/BarChart.jsx'
import ModelBreakdown from '../components/ModelBreakdown.jsx'
import RequestList from '../components/RequestList.jsx'
import { useT } from '../i18n/index.jsx'
import { ErrorNote, Loading, PageHead, StatCard, useLoad } from '../components/ui.jsx'
import {
  IconArrowRight, IconCalendar, IconCard, IconCoins, IconKey, IconPlus,
  IconTrendDown, IconTrendUp, IconZap,
} from '../components/icons.jsx'
import { fmtDateLong, fmtInt, fmtSom, MICRO } from '../format.js'

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
  const model = useLoad(() => api.get('/portal/status'), [], { refreshMs: 30_000 })
  const [tab, setTab] = useState('requests')
  const s = sum.data
  const low = s && s.balance - s.held < 1000 * MICRO

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
        action={<span className="date-pill"><IconCalendar /> {fmtDateLong()}</span>}
      />

      {model.data && !model.data.model_online && (
        <div className="alert alert--warn" style={{ marginBottom: 20 }}>{t('dash.offline')} <Link to="/status">{t('dash.statusPage')}</Link></div>
      )}
      <ErrorNote error={sum.error} retry={sum.reload} />
      {!s && !sum.error && <Loading />}
      {s && (
        <>
          <div className="balance-card">
            <div className="balance-big">
              {freeMode
                ? (<><span className="label">{t('dash.usageLabel')}</span><span className="figure">{t('common.free')}</span><span className="note">{t('dash.freeNote')}</span></>)
                : (<>
                    <span className="label">{t('dash.balance')}</span>
                    <span className="figure">{fmtSom(s.balance, { unit: false })}<small>{t('common.som')}</small></span>
                    <span className="note">{t('dash.balanceNote')}</span>
                  </>)}
            </div>
            {!freeMode && <Link className="btn btn--primary btn--large" to="/app/billing"><IconPlus /> {t('dash.topUp')} <IconArrowRight /></Link>}
          </div>

          <div className="metric-grid" style={{ marginTop: 18 }}>
            {!freeMode && (
              <StatCard icon={<IconCoins />} label={t('dash.spentMonth')}
                        value={fmtSom(s.month.cost, { unit: false })} unit={t('common.som')}
                        note={t('dash.currentMonth')} />
            )}
            <StatCard icon={<IconZap />} label={t('dash.requests')} value={fmtInt(s.month.requests)} unit={t('dash.thisMonth')}
                      trend={<><IconTrendUp /> {fmtInt(s.today.requests)}</>} trendDir="up" note={t('dash.today')} />
            <StatCard icon={<IconCard />} label={t('dash.tokens')} value={fmtInt(s.month.tokens)} note={t('dash.monthTotal')} />
            <StatCard icon={<IconKey />} label={t('dash.activeKeys')} value={fmtInt(s.active_keys)} unit={t('dash.active')}
                      note={<Link to="/app/keys" className="small">{t('dash.manage')}</Link>} />
            {!freeMode && s.held > 0 && (
              <StatCard icon={<IconCoins />} label={t('dash.held')} value={fmtSom(s.held, { unit: false })} unit={t('common.som')}
                        trend={<><IconTrendDown /> {t('dash.heldShort')}</>} note={t('dash.heldNote')} />
            )}
          </div>
          {low && !freeMode && <div className="alert alert--warn" style={{ marginTop: 18 }}>{t('dash.lowBalance', { link: <Link to="/app/billing">{t('dash.lowBalanceLink')}</Link> })}</div>}
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
          <div className="panel-head" style={{ margin: '22px 0 8px' }}>
            <div className="panel-head__t"><h2 style={{ fontSize: '1.05rem' }}>{t('dash.byModel')}</h2><p>{t('dash.byModelLead')}</p></div>
          </div>
          <ModelBreakdown days={30} freeMode={freeMode} />
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
