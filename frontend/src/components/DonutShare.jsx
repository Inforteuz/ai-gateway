import { useMemo, useState } from 'react'
import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { fmtInt, fmtNumber, fmtSom } from '../format.js'

/**
 * Share of usage per group (model, upstream, client) as a donut with a legend table.
 *
 * `rows`: [{label, requests, prompt_tokens, completion_tokens, cost}], largest token
 * share first (the server orders them). Colors follow the GROUP, not its rank under
 * the current measure: they are assigned once from the token order, so switching
 * Tokens <-> Cost resizes the segments but never repaints them. Groups past the six
 * palette slots fold into one neutral "Other" segment. The table carries every
 * value, so no color has to be read on its own.
 */
const SLOTS = 6
const R = 64
const WIDTH = 22

const tokens = (r) => (r.prompt_tokens || 0) + (r.completion_tokens || 0)
// Token counts run to seven digits; in full they crowded the cost column out of
// the card (2 290 958 next to 31 035 сум). The table shows 2.29M, the title the exact count.
const compact = (n) => (n >= 1e6 ? `${fmtNumber(n / 1e6, 2)}M` : n >= 1e4 ? `${fmtNumber(n / 1e3, 0)}K` : fmtInt(n))

function arc(cx, cy, r, a0, a1) {
  const p = (a) => [cx + r * Math.sin(a), cy - r * Math.cos(a)]
  const [x0, y0] = p(a0)
  const [x1, y1] = p(a1)
  return `M ${x0} ${y0} A ${r} ${r} 0 ${a1 - a0 > Math.PI ? 1 : 0} 1 ${x1} ${y1}`
}

export default function DonutShare({ title, hint, rows }) {
  useLocaleRefresh()
  const [metric, setMetric] = useState('tokens')
  const [hot, setHot] = useState(null)
  const value = (r) => (metric === 'cost' ? r.cost || 0 : tokens(r))
  const fmt = (n) => (metric === 'cost' ? fmtSom(n) : fmtInt(n))

  const groups = useMemo(() => {
    const named = rows.slice(0, SLOTS).map((r, i) => ({ ...r, key: `g${i}`, color: `var(--viz-${i + 1})`,
      name: r.label || tr('Belgilanmagan') }))
    const rest = rows.slice(SLOTS)
    if (rest.length) {
      named.push(rest.reduce((o, r) => ({ ...o, requests: o.requests + r.requests,
        prompt_tokens: o.prompt_tokens + r.prompt_tokens, completion_tokens: o.completion_tokens + r.completion_tokens,
        cost: o.cost + r.cost }), { key: 'other', name: tr('Boshqalar'), color: 'var(--fg-subtle)',
        requests: 0, prompt_tokens: 0, completion_tokens: 0, cost: 0 }))
    }
    return named
  }, [rows])

  const total = groups.reduce((s, g) => s + value(g), 0)
  let angle = 0
  const segments = total > 0 ? groups.map((g) => {
    const share = value(g) / total
    const seg = { ...g, share, a0: angle, a1: angle + share * 2 * Math.PI }
    angle = seg.a1
    return seg
  }).filter((s) => s.share > 0) : []
  const focus = segments.find((s) => s.key === hot)

  return (
    <section className="panel share-card">
      <div className="panel-head">
        <div className="panel-head__t"><h2>{title}</h2>{hint && <p>{hint}</p>}</div>
        <div className="seg" role="group" aria-label={tr('Grafik o‘lchovi')}>
          {['tokens', 'cost'].map((m) => <button key={m} className={metric === m ? 'is-on' : ''} onClick={() => setMetric(m)}>{tr(m === 'tokens' ? 'Tokenlar' : 'Sarf')}</button>)}
        </div>
      </div>
      {!segments.length ? <p className="muted">{tr("Bu davrda so'rov bo'lmagan.")}</p> : (
        <div className="share-card__body">
          <svg className="donut" viewBox="0 0 160 160" role="img" aria-label={title} onMouseLeave={() => setHot(null)}>
            {segments.length === 1
              ? <circle cx="80" cy="80" r={R} fill="none" stroke={segments[0].color} strokeWidth={WIDTH}
                        onMouseEnter={() => setHot(segments[0].key)} />
              : segments.map((s) => (
                <path key={s.key} d={arc(80, 80, R, s.a0, s.a1)} fill="none" stroke={s.color} strokeWidth={WIDTH}
                      className={`donut__seg ${hot && hot !== s.key ? 'is-dim' : ''}`}
                      onMouseEnter={() => setHot(s.key)} tabIndex={0} onFocus={() => setHot(s.key)}
                      aria-label={`${s.name}: ${fmt(value(s))} (${Math.round(s.share * 100)}%)`} />
              ))}
            {segments.length > 1 && segments.map((s) => (
              // 2px surface gap between neighbours, so adjacent hues never touch
              <line key={`gap-${s.key}`} className="donut__gap" x1={80 + (R - WIDTH / 2 - 1) * Math.sin(s.a0)} y1={80 - (R - WIDTH / 2 - 1) * Math.cos(s.a0)}
                    x2={80 + (R + WIDTH / 2 + 1) * Math.sin(s.a0)} y2={80 - (R + WIDTH / 2 + 1) * Math.cos(s.a0)} />
            ))}
            <text className="donut__value" x="80" y="80">{focus ? `${Math.round(focus.share * 100)}%` : fmt(total)}</text>
            <text className="donut__label" x="80" y="98">{focus ? focus.name : tr(metric === 'cost' ? 'Jami sarf' : 'Jami tokenlar')}</text>
          </svg>
          <table className="table share-table">
            <thead><tr><th>{tr('Guruh')}</th><th className="num">{tr("So'rovlar")}</th><th className="num">{tr('Tokenlar')}</th><th className="num">{tr('Sarf')}</th></tr></thead>
            <tbody>
              {groups.map((g) => (
                <tr key={g.key} className={hot === g.key ? 'is-hot' : ''} onMouseEnter={() => setHot(g.key)} onMouseLeave={() => setHot(null)}>
                  <td><span className="share-table__name"><i style={{ background: g.color }} aria-hidden="true" /><span title={g.name}>{g.name}</span></span></td>
                  <td className="num">{fmtInt(g.requests)}</td>
                  <td className="num" title={fmtInt(tokens(g))}>{compact(tokens(g))}</td>
                  <td className="num">{fmtSom(g.cost)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
