import { translateText as tr } from '../i18n/index.jsx'
import { useState } from 'react'
import { fmtDay, fmtInt, fmtSom } from '../format.js'
import { useT } from '../i18n/index.jsx'

/** `items`: [{date, cost, requests, prompt_tokens, completion_tokens}] */
const pick = (d, metric) => (metric === 'tokens' ? (d.prompt_tokens || 0) + (d.completion_tokens || 0) : d[metric] || 0)

/** A readable "round" upper bound and ~4 tick steps. */
function scale(max) {
  if (max <= 0) return { top: 1, ticks: [0, 1] }
  const pow = 10 ** Math.floor(Math.log10(max))
  const step = [1, 2, 2.5, 5, 10].find((s) => s * pow >= max / 4) * pow
  const top = Math.ceil(max / step) * step
  const ticks = []
  for (let v = 0; v <= top + 1e-9; v += step) ticks.push(v)
  return { top, ticks }
}

/** Daily usage chart — the bar chart from the mockup. */
export default function BarChart({ items, metric = 'cost', compact = false }) {
  const t = useT()
  const [hot, setHot] = useState(null)
  const W = 720, H = compact ? 300 : 240
  const pad = compact ? { l: 76, r: 10, t: 22, b: 52 } : { l: 42, r: 8, t: 14, b: 28 }
  const vals = items.map((d) => pick(d, metric))
  const { top, ticks } = scale(Math.max(...vals, 0))
  const plotW = W - pad.l - pad.r
  const plotH = H - pad.t - pad.b
  const bw = plotW / Math.max(items.length, 1)
  const cur = hot ?? items.length - 1
  const d = items[cur]
  const label = (x) => (metric === 'cost' ? fmtSom(x) : metric === 'tokens' ? t('chart.tokens', { n: fmtInt(x) }) : t('chart.requests', { n: fmtInt(x) }))
  const axisEvery = Math.max(1, Math.ceil(items.length / 8))

  return (
    <div className={`chart ${compact ? 'chart--compact' : ''}`}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={t('chart.aria')} onMouseLeave={() => setHot(null)}>
        {ticks.map((t) => {
          const y = pad.t + plotH - (t / top) * plotH
          return (
            <g key={t}>
              <line className="grid" x1={pad.l} x2={W - pad.r} y1={y} y2={y} />
              <text x={pad.l - 12} y={y + (compact ? 7 : 3.5)} textAnchor="end">{tr(metric === 'cost' ? fmtInt(t / 1_000_000) : fmtInt(t))}</text>
            </g>
          )
        })}
        {items.map((it, i) => {
          const v = pick(it, metric)
          const h = Math.max((v / top) * plotH, v === 0 ? 1.5 : 2)
          return (
            <g key={it.date} onMouseEnter={() => setHot(i)} onFocus={() => setHot(i)} tabIndex={0}
               aria-label={`${fmtDay(it.date)}: ${label(v)}`}>
              <rect x={pad.l + i * bw} y={pad.t} width={bw} height={plotH} fill="transparent" />
              <rect className={`bar ${v === 0 ? 'bar--zero' : ''} ${i === cur ? 'is-hot' : ''}`}
                    x={pad.l + i * bw + bw * 0.2} width={Math.max(bw * 0.6, 3)} rx="3"
                    y={pad.t + plotH - h} height={h} />
            </g>
          )
        })}
        {items.map((it, i) => (i === items.length - 1
          || (i % axisEvery === 0 && i < items.length - 1 - Math.floor(axisEvery / 2))) && (
          <text key={it.date} className="xlab" x={pad.l + i * bw + bw / 2} y={H - (compact ? 14 : 8)} textAnchor="middle">{fmtDay(it.date)}</text>
        ))}
      </svg>
      {d && <div className="chart-tip" aria-live="polite"><b>{fmtDay(d.date)}</b>: {label(pick(d, metric))}{tr(metric === 'cost' ? `, ${t('chart.requests', { n: fmtInt(d.requests) })}` : '')}</div>}
    </div>
  )
}
