import { useState } from 'react'
import { fmtInt } from '../format.js'

/**
 * Price vs. score: a scatter plot.
 *
 * Why a scatter: price and score are two different measures. Showing them in
 * a single bar chart with two axes is the most common charting mistake
 * (stretch two y-axes to fit each other and any conclusion "emerges").
 * In a scatter each measure sits on its own axis and nothing is stretched.
 *
 * Single series, so no legend is needed: the heading says it all.
 * Each point is labeled with the model name: there are only 7 points, so
 * labeling all of them does not hurt readability.
 */
export default function PriceScoreChart({ points }) {
  const [hover, setHover] = useState(null)
  if (!points?.length) return null

  const W = 760, H = 380
  const pad = { top: 24, right: 28, bottom: 62, left: 52 }
  const innerW = W - pad.left - pad.right
  const innerH = H - pad.top - pad.bottom

  // X axis: price. The upper bound is rounded so ticks land on whole numbers
  // and the most expensive point doesn't stick to the edge.
  const maxPrice = Math.max(...points.map((p) => p.price)) || 1
  const step = maxPrice > 60000 ? 30000 : maxPrice > 20000 ? 10000 : 5000
  const xMax = Math.ceil(maxPrice / step) * step + step / 2
  const xTicks = []
  for (let v = 0; v <= xMax; v += step) xTicks.push(v)

  const X = (price) => pad.left + (price / xMax) * innerW
  const Y = (ratio) => pad.top + innerH - ratio * innerH

  return (
    <figure className="viz" style={{ margin: 0 }}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
           aria-label="Modellarning narxi va sinov natijasi">
        {/* Y axis: score percentage */}
        {[0, 25, 50, 75, 100].map((t) => (
          <g key={t}>
            <line x1={pad.left} y1={Y(t / 100)} x2={W - pad.right} y2={Y(t / 100)}
                  stroke="var(--border)" strokeWidth="1" />
            <text x={pad.left - 8} y={Y(t / 100) + 4} textAnchor="end"
                  fontSize="11" fill="var(--fg-subtle)">{t}%</text>
          </g>
        ))}

        {/* X axis: price */}
        {xTicks.map((v) => (
          <text key={v} x={X(v)} y={H - pad.bottom + 20} textAnchor="middle"
                fontSize="11" fill="var(--fg-subtle)">
            {v === 0 ? 'bepul' : fmtInt(v / 1000) + 'k'}
          </text>
        ))}
        <text x={pad.left + innerW / 2} y={H - 14} textAnchor="middle"
              fontSize="11" fill="var(--fg-muted)">
          1 million chiqish tokeni uchun narx, so'm
        </text>

        {points.map((p) => {
          const isHovered = hover === p.id
          return (
            <g key={p.id}
               onMouseEnter={() => setHover(p.id)}
               onMouseLeave={() => setHover(null)}>
              {/* Hit area larger than the dot, so the pointer needn't be exact */}
              <circle cx={X(p.price)} cy={Y(p.ratio)} r="18" fill="transparent" />
              {/* Background-colored ring keeps overlapping dots distinguishable */}
              <circle cx={X(p.price)} cy={Y(p.ratio)} r={isHovered ? 8 : 6}
                      fill="var(--viz-1)" stroke="var(--bg-elev)" strokeWidth="2" />
              <text x={X(p.price)} y={Y(p.ratio) - 14} textAnchor="middle"
                    fontSize="11" fontWeight={isHovered ? 600 : 400}
                    fill={isHovered ? 'var(--fg)' : 'var(--fg-muted)'}>{p.id}</text>
              <title>
                {`${p.id} — ${Math.round(p.ratio * 100)}% (${p.correct}/${p.total}), `
                 + (p.price ? `${fmtInt(p.price)} so'm / 1M token` : 'bepul')}
              </title>
            </g>
          )
        })}
      </svg>
      <figcaption className="muted small" style={{ marginTop: 6 }}>
        Chap yuqoridagi nuqta eng foydali: arzon, lekin natijasi yuqori.
        Nuqta ustiga sichqonchani olib borsangiz aniq son chiqadi.
      </figcaption>
    </figure>
  )
}
