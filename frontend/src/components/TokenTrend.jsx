import { useState } from 'react'
import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { fmtDay, fmtInt } from '../format.js'
import { scale } from './BarChart.jsx'

/**
 * Input and output tokens per day as two lines on ONE axis (same unit, so no second
 * scale). `items`: [{date, prompt_tokens, completion_tokens}]. A crosshair follows
 * the pointer and the readout under the plot names both values for that day.
 */
const SERIES = [
  { key: 'prompt_tokens', name: 'Kirish tokenlari', color: 'var(--viz-1)' },
  { key: 'completion_tokens', name: 'Chiqish tokenlari', color: 'var(--viz-2)' },
]

export default function TokenTrend({ items }) {
  useLocaleRefresh()
  const [hot, setHot] = useState(null)
  const W = 720, H = 260
  const pad = { l: 52, r: 12, t: 14, b: 28 }
  const plotW = W - pad.l - pad.r
  const plotH = H - pad.t - pad.b
  const { top, ticks } = scale(Math.max(0, ...items.flatMap((d) => SERIES.map((s) => d[s.key] || 0))))
  const step = plotW / Math.max(items.length - 1, 1)
  const x = (i) => pad.l + i * step
  const y = (v) => pad.t + plotH - (v / top) * plotH
  const cur = hot ?? items.length - 1
  const d = items[cur]
  const axisEvery = Math.max(1, Math.ceil(items.length / 8))
  const short = (v) => (v >= 1e6 ? `${fmtInt(Math.round(v / 1e5) / 10)}M` : v >= 1e3 ? `${fmtInt(Math.round(v / 100) / 10)}K` : fmtInt(v))

  const onMove = (e) => {
    const box = e.currentTarget.getBoundingClientRect()
    const px = ((e.clientX - box.left) / box.width) * W
    setHot(Math.min(items.length - 1, Math.max(0, Math.round((px - pad.l) / step))))
  }

  return (
    <section className="panel share-card">
      <div className="panel-head">
        <div className="panel-head__t"><h2>{tr('Tokenlar trendi')}</h2><p>{tr('Kunlik kirish va chiqish tokenlari')}</p></div>
        <div className="trend-legend">
          {SERIES.map((s) => <span key={s.key}><i style={{ background: s.color }} aria-hidden="true" />{tr(s.name)}</span>)}
        </div>
      </div>
      <div className="chart">
        <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={tr('Tokenlar trendi')} onMouseMove={onMove} onMouseLeave={() => setHot(null)}>
          {ticks.map((t) => (
            <g key={t}>
              <line className="grid" x1={pad.l} x2={W - pad.r} y1={y(t)} y2={y(t)} />
              <text x={pad.l - 10} y={y(t) + 3.5} textAnchor="end">{short(t)}</text>
            </g>
          ))}
          {d && <line className="trend-cross" x1={x(cur)} x2={x(cur)} y1={pad.t} y2={pad.t + plotH} />}
          {SERIES.map((s) => (
            <polyline key={s.key} className="trend-line" stroke={s.color}
                      points={items.map((it, i) => `${x(i)},${y(it[s.key] || 0)}`).join(' ')} />
          ))}
          {d && SERIES.map((s) => <circle key={s.key} className="trend-dot" cx={x(cur)} cy={y(d[s.key] || 0)} r="4" fill={s.color} />)}
          {items.map((it, i) => (i === items.length - 1
            || (i % axisEvery === 0 && i < items.length - 1 - Math.floor(axisEvery / 2))) && (
            <text key={it.date} className="xlab" x={x(i)} y={H - 8} textAnchor="middle">{fmtDay(it.date)}</text>
          ))}
        </svg>
        {d && <div className="chart-tip" aria-live="polite"><b>{fmtDay(d.date)}</b>: {SERIES.map((s) => `${tr(s.name)} ${fmtInt(d[s.key])}`).join(', ')}</div>}
      </div>
    </section>
  )
}
