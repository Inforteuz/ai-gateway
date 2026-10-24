import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth, useSite } from '../auth.js'
import { META_FIELD, MODEL_PLACEHOLDER, apiOrigin, freeModel, hostOf } from '../site.js'
import { CategoryChart, OverallChart } from '../components/BenchmarkChart.jsx'
import DemoReceipt from '../components/DemoReceipt.jsx'
import { CodeTabs, Status, useLoad } from '../components/ui.jsx'
import { MICRO, fmtNumber } from '../format.js'
import { snippets } from './snippets.js'
import { localeOf, useLang, useT } from '../i18n/index.jsx'

// Sign-up gift for new users. The visible counterpart of the server's
// `signup_bonus_micro` setting — update this number when that setting changes.
const BONUS_SOM = 10_000

const FALLBACK = {
  id: MODEL_PLACEHOLDER, display_name: MODEL_PLACEHOLDER, description: '',
  price_in_per_1m: 0, price_out_per_1m: 0,
  context_window: 16384, capabilities: { verified_answers: false, conditional_verification: false }, data_processing: 'private',
}

function Icon({ name }) {
  useLocaleRefresh()
  const p = { fill: 'none', stroke: 'currentColor', strokeWidth: 1.75, strokeLinecap: 'round', strokeLinejoin: 'round', viewBox: '0 0 24 24' }
  switch (name) {
    case 'bolt': return <svg {...p}><path d="M13 2L4 14h7l-1 8 9-12h-7l1-8z" /></svg>
    case 'shield': return <svg {...p}><path d="M12 2l8 3v6c0 5-3.5 9-8 11-4.5-2-8-6-8-11V5l8-3z" /><path d="M9 12l2 2 4-4" /></svg>
    case 'receipt': return <svg {...p}><path d="M5 3h14v18l-3-2-3 2-3-2-3 2-2-2V3z" /><path d="M9 8h6M9 12h6M9 16h4" /></svg>
    case 'stream': return <svg {...p}><path d="M3 7h18M5 12h14M7 17h10" /></svg>
    case 'brain': return <svg {...p}><path d="M9 3a3 3 0 00-3 3v1a3 3 0 00-2 5 3 3 0 002 5v1a3 3 0 006 0V3a3 3 0 00-3 0zM15 3a3 3 0 013 3v1a3 3 0 012 5 3 3 0 01-2 5v1a3 3 0 01-6 0V3a3 3 0 013 0z" /></svg>
    case 'stack': return <svg {...p}><path d="M12 3l9 5-9 5-9-5 9-5z" /><path d="M3 13l9 5 9-5M3 18l9 5 9-5" /></svg>
    case 'globe': return <svg {...p}><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c2.5 2.6 2.5 15.4 0 18M12 3c-2.5 2.6-2.5 15.4 0 18" /></svg>
    case 'gift': return <svg {...p}><path d="M3 11h18v10H3zM3 7h18v4H3z" /><path d="M12 7v14" /><path d="M12 7S10.5 3 8.5 3a2 2 0 000 4zM12 7s1.5-4 3.5-4a2 2 0 010 4z" /></svg>
    case tr("code"): return <svg {...p}><path d="M9 18l-6-6 6-6M15 6l6 6-6 6" /></svg>
    case 'rocket': return <svg {...p}><path d="M5 15c-1.5 2-1.5 5-1.5 5s3 0 5-1.5" /><path d="M9 15l-3-3c0-6 4-10 11-10 0 7-4 11-10 11l-1 2z" /><circle cx="14.5" cy="9.5" r="1.5" /></svg>
    case 'terminal': return <svg {...p}><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 9l3 3-3 3M13 15h4" /></svg>
    case 'arrow': return <svg {...p} width="14" height="14"><path d="M5 12h14M13 6l6 6-6 6" /></svg>
    case 'check': return <svg {...p}><path d="M4 12l5 5L20 6" /></svg>
    default: return null
  }
}

// Texts live in the dictionaries under `landing.feat.<id>.*` / `landing.aud.<id>.*`.
const FEATURES = [
  { icon: 'bolt', id: 'sdk' },
  { icon: 'receipt', id: 'billing' },
  { icon: 'globe', id: 'uzbek' },
  { icon: 'gift', id: 'free' },
  { icon: 'shield', id: 'retry' },
  { icon: 'stack', id: 'catalog' },
]

const AUDIENCE = [
  { icon: 'terminal', id: 'devs' },
  { icon: 'rocket', id: 'startups' },
  { icon: 'code', id: 'agents' },
]

/** Finds the "best" model in benchmark data — numbers come only from measurements. */
function best(items, category) {
  let top = null
  for (const m of items) {
    const r = category ? m.scores?.[category]?.ratio : m.ratio
    if (r == null) continue
    if (!top || r > top.ratio) top = { id: m.id, name: m.display_name || m.id, ratio: r }
  }
  return top
}

/** Fastest model — only from real measurements (median of recent requests) and only if reliable.
 *  A model returning many errors is not shown as "fast": a fast error is not a win. */
function fastest(items) {
  let top = null
  for (const m of items) {
    const o = m.measured
    if (!o?.typical_latency_ms || (o.success_rate ?? 0) < 0.9) continue
    if (!top || o.typical_latency_ms < top.ms) top = { id: m.id, name: m.display_name || m.id, ms: o.typical_latency_ms }
  }
  return top
}

const secs = (ms) => fmtNumber(Math.round(ms / 100) / 10, 1)

export default function Landing() {
  useLocaleRefresh()
  const t = useT()
  const { lang } = useLang()
  const bench = useLoad(() => api.get('/portal/benchmark'), [])
  const { user, freeMode, registrationOpen, loading: authLoading } = useAuth()
  const { data: models } = useLoad(() => api.get('/portal/models'), [])
  const site = useSite()
  const origin = apiOrigin(site)
  const { data: status } = useLoad(() => api.get('/portal/status'), [])
  const list = models?.items?.length ? models.items : [FALLBACK]
  const main = list[0]
  const limits = models?.limits
  // Free model's daily cap and referral terms come from admin settings (never hard-coded)
  const dailyTokens = limits?.free_daily_tokens || 0
  // Short form (hero, CTA); the full condition sits in the pricing notes
  const freeNote = dailyTokens ? t('landing.freeDaily', { n: fmtNumber(dailyTokens) }) : t('landing.freeWord')
  const referral = models?.referral
  const refPercent = referral?.enabled && referral.percent > 0 && !freeMode ? referral.percent : 0
  const bonus = fmtNumber(BONUS_SOM)

  // With sign-up closed, the gift and "open an account yourself" texts make no sense.
  // Until the setting arrives we assume it's open: otherwise the first frame would show
  // a different offer and then switch.
  const signupOpen = (authLoading || registrationOpen) && !freeMode
  const signupLink = user ? null : (signupOpen ? '/login?mode=register' : '/login')
  const maxCtx = Math.max(...list.map((m) => m.context_window || 0))
  // The page always shows the best measured result — recomputed on every load
  const fast = fastest(list)
  // Only from the real catalog (so the placeholder model never shows up as "free" while loading)
  const freeM = freeModel(models?.items)
  // Use a paid model so the receipt shows a real amount; otherwise the first one.
  const receiptModel = list.find((m) => m.price_in_per_1m || m.price_out_per_1m) || main
  const som = t('common.som')

  return (
    <>
      <section className="hero-stage">
        <span className="hero-blob" aria-hidden="true" />
        <div className="wrap">
          <div className="hero">
            <div className="hero-copy">
              <span className="badge">
                <span className="pulse" />
                {signupOpen
                  ? <><b>{t('landing.badgeFree')}</b> · {t('landing.badgeBonus', { bonus })}</>
                  : <><b>v2.0</b> · {t('landing.badgeBilling')}</>}
              </span>
              <h1>
                {tr(freeMode
                  ? t('landing.h1Free', { accent: <span className="serif">{t('landing.h1FreeAccent')}</span> })
                  : t('landing.h1', { accent: <span className="serif">{t('landing.h1Accent')}</span> }))}
              </h1>
              <p>
                {tr(freeMode
                  ? t('landing.leadFree')
                  : <>
                      {t('landing.lead', { code: <code>base_url</code> })}
                      {signupOpen && <> {t('landing.leadBonus', {
                        start: freeM ? `${freeM.display_name} ${freeNote}` : t('landing.startFree'),
                        bonus,
                      })}</>}
                    </>)}
              </p>
              <div className="hero-actions">
                <Link className="btn btn--primary btn--large" to={signupLink || '/app'}>
                  {tr(user ? t('nav.toPanel') : (signupOpen ? t('landing.ctaStart') : t('nav.login')))} <Icon name="arrow" />
                </Link>
                <Link className="btn btn--large" to="/docs">{t('nav.docs')}</Link>
              </div>
              <div className="tick-list">
                <span className="tick"><i>&#10003;</i> {t('landing.tick1')}</span>
                <span className="tick"><i>&#10003;</i> {t('landing.tick2')}</span>
                <span className="tick"><i>&#10003;</i> {t('landing.tick3')}</span>
              </div>
              {status && (
                <div style={{ marginTop: 2 }}>
                  <Status online={status.model_online} />
                </div>
              )}
            </div>

            <div className="hero-visual">
              <div className="hero-mock-glow" aria-hidden="true" />
              <div className="hero-mock" role="img" aria-label={t('landing.mockAria', { brand: site.brand_name })}>
                <div className="hero-mock-bar">
                  <span className="hero-mock-dot" style={{ background: '#fb7185' }} />
                  <span className="hero-mock-dot" style={{ background: '#fbbf24' }} />
                  <span className="hero-mock-dot" style={{ background: '#34d399' }} />
                  <span className="hero-mock-url">{hostOf(origin)}/v1/chat/completions</span>
                </div>
                <div className="hero-mock-body">
                  <div className="hero-mock-code">
                    <div><span className="c"># {t('landing.mockComment')}</span></div>
                    <div><span className="k">{tr("from")}</span> {tr("openai")} <span className="k">{tr("import")}</span> OpenAI</div>
                    <div><span className="p">{tr("client")}</span> = OpenAI(</div>
                    <div>&nbsp;&nbsp;base_url=<span className="s">"{tr(origin)}/v1"</span>,</div>
                    <div>&nbsp;&nbsp;api_key=<span className="s">"sk-sf-…"</span></div>
                    <div>)</div>
                    <div style={{ marginTop: 6 }}><span className="p">{tr("res")}</span> = client.chat.completions.create(</div>
                    <div>&nbsp;&nbsp;model=<span className="s">"{tr(main.id)}"</span>,</div>
                    <div>&nbsp;&nbsp;messages=[…],</div>
                    <div>&nbsp;&nbsp;stream=<span className="k">{tr("True")}</span>,</div>
                    <div>)</div>
                  </div>
                  <div className="hero-mock-stats">
                    {fast
                      ? <div className="hero-mock-stat"><b>{secs(fast.ms)}<small style={{ fontSize: '0.5em', marginLeft: 4, color: 'var(--fg-subtle)', fontWeight: 500 }}>{t('common.secUnit')}</small></b><span>{t('landing.mockReply', { name: fast.name })}</span></div>
                      : <div className="hero-mock-stat"><b>OpenAI</b><span>{t('landing.compatible')}</span></div>}
                    <div className="hero-mock-stat"><b>UZS</b><span>{t('landing.mockBilling')}</span></div>
                    <div className="hero-mock-stat"><b>SSE</b><span>{t('landing.mockStream')}</span></div>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </section>

      <section className="section">
        <div className="wrap">
          <div className="stats">
            {signupOpen
              ? <div className="stat"><b>{tr(bonus)}</b><span>{t('landing.statBonus')}</span></div>
              : <div className="stat"><b>OpenAI</b><span>{t('landing.compatible')}</span></div>}
            {freeM && <div className="stat"><b>0 {tr(som)}</b><span>{t('landing.statFreePrice', { name: freeM.display_name })}</span></div>}
            <div className="stat"><b>{Math.round(maxCtx / 1000)}k</b><span>{t('landing.statContext')}</span></div>
            <div className="stat"><b>24/7</b><span>{t('landing.statMonitoring')}</span></div>
          </div>
        </div>
      </section>

      <section className="section" id="imkoniyatlar">
        <div className="wrap">
          <div className="section-head">
            <span className="eyebrow">{t('landing.whyEyebrow')}</span>
            <h2>{t('landing.whyTitle')}</h2>
            <p>{t('landing.whyLead')}</p>
          </div>
          <div className="feature-grid">
            {FEATURES.map((f, i) => (
              <article key={f.icon} className={`feature ${i === 0 ? 'feature--highlight' : ''}`}>
                <div className="feature-ico"><Icon name={f.icon} /></div>
                <h3>{t(`landing.feat.${f.id}.title`)}</h3>
                <p>{t(`landing.feat.${f.id}.text`, { bonus })}</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="section" id="kimlar-uchun">
        <div className="wrap">
          <div className="section-head">
            <span className="eyebrow">{t('landing.audEyebrow')}</span>
            <h2>{t('landing.audTitle')}</h2>
            <p>{t('landing.audLead')}</p>
          </div>
          <div className="feature-grid">
            {AUDIENCE.map((a) => (
              <article key={a.icon} className="feature">
                <div className="feature-ico"><Icon name={a.icon} /></div>
                <h3>{t(`landing.aud.${a.id}.title`)}</h3>
                <p>{t(`landing.aud.${a.id}.text`, { bonus })}</p>
              </article>
            ))}
          </div>
        </div>
      </section>

      <section className="section" id="sinov">
        <div className="wrap">
          {(() => {
            // Models missing from the catalog (testing or disabled) are hidden from the charts.
            const catalogIds = models?.items?.length ? new Set(models.items.map((m) => m.id)) : null
            const items = (bench.data?.items || []).filter((m) => !catalogIds || catalogIds.has(m.id))
            const categories = bench.data?.categories || []
            if (!items.length) return null
            const overall = best(items)
            // Category names are benchmark data from the server, not UI text
            const code = best(items, 'Kod')
            const uzbek = best(items, tr("O'zbek tili"))
            const total = items[0]?.total
            const pct = (x) => `${Math.round(x.ratio * 100)}%`
            return (
              <>
                <div className="section-head">
                  <span className="eyebrow">{t('landing.benchEyebrow')}</span>
                  <h2>{t('landing.benchTitle')}</h2>
                  <p>
                    {tr(total ? t('landing.benchTasksN', { n: total }) : t('landing.benchTasks'))}
                    {tr(categories.length ? t('landing.benchCats', { n: categories.length, list: categories.join(', ').toLowerCase() }) : '')}.
                    {' '}{t('landing.benchRest')}
                  </p>
                </div>
                <div className="stats" style={{ marginBottom: 14 }}>
                  {overall && <div className="stat"><b>{pct(overall)}</b><span>{t('landing.benchOverall', { name: overall.name })}</span></div>}
                  {code && <div className="stat"><b>{pct(code)}</b><span>{t('landing.benchCode', { name: code.name })}</span></div>}
                  {uzbek && <div className="stat"><b>{pct(uzbek)}</b><span>{t('landing.benchUzbek', { name: uzbek.name })}</span></div>}
                  {fast && <div className="stat"><b>{t('common.seconds', { n: secs(fast.ms) })}</b><span>{t('landing.benchLatency', { name: fast.name })}</span></div>}
                </div>
                <div className="panel">
                  <OverallChart items={items} />
                </div>
                <div className="panel" style={{ marginTop: 14 }}>
                  <h3 style={{ marginTop: 0, fontSize: '1rem' }}>{t('landing.benchByCategory')}</h3>
                  <CategoryChart items={items} categories={categories} />
                </div>
                <p className="muted small" style={{ marginTop: 10 }}>
                  {t('landing.benchNote')}
                  {bench.data?.updated_at && <>
                    {' '}{t('landing.benchUpdated', { date: new Date(bench.data.updated_at * 1000).toLocaleDateString(localeOf(lang)) })}
                  </>}
                  {fast && <>
                    {' '}{t('landing.benchLatencyNote')}
                  </>}
                </p>
              </>
            )
          })()}
        </div>
      </section>

      <section className="section" id="qanday">
        <div className="wrap showcase">
          <div className="section-head" style={{ marginBottom: 0 }}>
            <span className="eyebrow">{t('landing.stepsEyebrow')}</span>
            <h2>{tr(freeMode || !signupOpen ? t('landing.stepsTwo') : t('landing.stepsThree'))}</h2>
            <p>{t('landing.stepsLead')}</p>
          </div>
          <ol className="steps">
            {signupOpen
              ? <li><div><h3>{t('landing.step1Title')}</h3><p>{t('landing.step1Text', { bonus })}</p></div></li>
              : <li><div><h3>{t('landing.step1ClosedTitle')}</h3><p>{t('landing.step1ClosedText')}</p></div></li>}
            <li><div><h3>{t('landing.step2Title')}</h3><p>{t('landing.step2Text', { code: <code>api_key</code> })}</p></div></li>
            <li>
              <div>
                <h3>{t('landing.step3Title')}</h3>
                <p>{tr(freeMode ? t('landing.step3TextFree') : t('landing.step3Text'))}</p>
              </div>
            </li>
          </ol>
        </div>
      </section>

      <section className="section">
        <div className="wrap showcase">
          <div className="section-head" style={{ marginBottom: 0 }}>
            <span className="eyebrow">{t('landing.intEyebrow')}</span>
            <h2>{t('landing.intTitle')}</h2>
            <p>{t('landing.intLead')}</p>
          </div>
          <CodeTabs tabs={snippets(origin, 'sk-sf-...', main.id)} />
        </div>
      </section>

      <section className="section" id="narxlar">
        <div className="wrap">
          <div className="section-head">
            <span className="eyebrow">{tr(freeMode ? t('landing.priceEyebrowFree') : t('landing.priceEyebrow'))}</span>
            <h2>{tr(freeMode ? t('landing.priceTitleFree') : t('landing.priceTitle'))}</h2>
            <p>{tr(freeMode ? t('landing.priceLeadFree') : t('landing.priceLead'))}</p>
          </div>
          <div className="price-list">
            {list.map((m) => {
              const isFree = !m.price_in_per_1m && !m.price_out_per_1m
              return (
                <div className="price-row" key={m.id}>
                  <div>
                    <div className="price-model">{tr(m.display_name)}</div>
                    <div className="dim small" style={{ marginTop: 2 }}>
                      {tr(m.id)} · {t('landing.context', { n: fmtNumber(m.context_window) })}
                    </div>
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 8 }}>
                      {isFree && !freeMode && <span className="tag tag--ok">{t('common.free')}</span>}
                      {m.capabilities?.conditional_verification && <span className="tag tag--ok" title={t('landing.tagVerifyTitle', { field: `${META_FIELD}.verified` })}>{t('landing.tagVerify')}</span>}
                      {m.capabilities?.vision && <span className="tag tag--new">{t('landing.tagVision')}</span>}
                      {m.capabilities?.file_input && <span className="tag tag--new">{t('landing.tagFiles')}</span>}
                      {m.capabilities?.image_generation && <span className="tag tag--new">{t('landing.tagImages')}</span>}
                      {m.capabilities?.tool_calling && <span className="tag tag--new">{t('landing.tagTools')}</span>}
                      {m.data_processing === 'third_party' && <span className="tag tag--warn">{t('landing.tagThirdParty')}</span>}
                    </div>
                    {m.description && <p className="muted small" style={{ marginTop: 10, maxWidth: '32rem' }}>{tr(m.description)}</p>}
                  </div>
                  {freeMode
                    ? <div className="price-figs"><div><b>{t('common.free')}</b><span>{t('landing.forPartners')}</span></div></div>
                    : isFree
                      ? <div className="price-figs"><div><b>{t('common.free')}</b><span>{t('landing.noCharge')}</span></div></div>
                      : (
                        <div className="price-figs">
                          <div><b>{fmtNumber(m.price_in_per_1m / MICRO)} {tr(som)}</b><span>{t('landing.per1mIn')}</span></div>
                          <div><b>{fmtNumber(m.price_out_per_1m / MICRO)} {tr(som)}</b><span>{t('landing.per1mOut')}</span></div>
                        </div>
                      )}
                </div>
              )
            })}
          </div>
          <ul className="muted small" style={{ margin: '20px 0 0', paddingLeft: '1.2em', display: 'grid', gap: 6 }}>
            {signupOpen && <li>{t('landing.noteBonus', { bonus })}</li>}
            {!freeMode && <li>{t('landing.noteNoCharge')}</li>}
            {dailyTokens > 0 && !freeMode && <li>{t('landing.noteDaily', { model: freeM ? freeM.display_name : t('landing.freeModel'), n: fmtNumber(dailyTokens) })}</li>}
            <li>{t('landing.noteVerify')}</li>
            {!freeMode && <li>{t('landing.noteHold')}</li>}
            {limits && <li>{t('landing.noteLimits', { rpm: limits.default_rpm, n: fmtNumber(limits.max_max_tokens) })}</li>}
          </ul>
        </div>
      </section>

      {refPercent > 0 && (
        <section className="section" id="referal">
          <div className="wrap showcase">
            <div className="section-head" style={{ marginBottom: 0 }}>
              <span className="eyebrow">{t('landing.refEyebrow')}</span>
              <h2>{t('landing.refTitle', { pct: fmtNumber(refPercent) })}</h2>
              <p>{t('landing.refLead', { pct: fmtNumber(refPercent) })}</p>
              <div style={{ marginTop: 18 }}>
                <Link className="btn btn--primary" to={user ? '/app/referral' : signupLink}>
                  {tr(user ? t('landing.refGetLink') : t('landing.refSignup'))} <Icon name="arrow" />
                </Link>
              </div>
            </div>
            <ol className="steps">
              <li><div><h3>{t('landing.ref1Title')}</h3><p>{t('landing.ref1Text')}</p></div></li>
              <li><div><h3>{t('landing.ref2Title')}</h3><p>{t('landing.ref2Text')}</p></div></li>
              <li><div><h3>{t('landing.ref3Title')}</h3><p>{t('landing.ref3Text', { pct: fmtNumber(refPercent) })}</p></div></li>
            </ol>
          </div>
        </section>
      )}

      <section className="section">
        <div className="wrap">
          <div className="cta">
            <div>
              <h2>{tr(signupOpen ? t('landing.ctaTitleBonus', { bonus }) : t('landing.ctaTitle'))}</h2>
              <p>
                {tr(signupOpen
                  ? t('landing.ctaLeadBonus', { start: freeM ? t('landing.ctaStartModel', { name: freeM.display_name, note: freeNote }) : t('landing.ctaStartFree') })
                  : t('landing.ctaLead'))}
              </p>
            </div>
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              <Link className="btn btn--primary btn--large" to={user ? '/app/keys' : signupLink}>
                {tr(user ? t('landing.ctaCreateKey') : (signupOpen ? t('landing.ctaSignup') : t('nav.login')))} <Icon name="arrow" />
              </Link>
              <Link className="btn btn--large" to="/docs">{t('landing.ctaDocs')}</Link>
            </div>
          </div>
        </div>
      </section>

      <section className="wrap" aria-hidden="true" style={{ display: 'flex', justifyContent: 'center', padding: '0 24px 60px' }}>
        <div style={{ maxWidth: 420, width: '100%' }}>
          <DemoReceipt priceIn={receiptModel.price_in_per_1m} priceOut={receiptModel.price_out_per_1m} model={receiptModel.id} free={freeMode} />
        </div>
      </section>
    </>
  )
}
