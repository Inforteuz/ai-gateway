import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { useState } from 'react'
import { useT } from '../i18n/index.jsx'

// Category colors: fixed order, never rotated.
// Values come from a validated palette: adjacent pairs stay distinguishable
// even with color vision deficiency.
const SERIES = [
  { light: '#2a78d6', dark: '#3987e5' },  // blue
  { light: '#eb6834', dark: '#d95926' },  // orange
  { light: '#1baf7a', dark: '#199e70' },  // teal
  { light: '#eda100', dark: '#c98500' },  // yellow
  { light: '#e87ba4', dark: '#d55181' },  // pink
  { light: '#008300', dark: '#008300' },  // green
]

const BAR_MAX = 24        // max bar thickness
const GAP = 2             // gap between adjacent bars
const RADIUS = 4          // bar tip corner radius

/** Bar: rounded tip, square base (flush with the baseline). */
function Bar({ x, y, w, h, fill, opacity = 1 }) {
  useLocaleRefresh()
  if (h <= 0) return null
  const r = Math.min(RADIUS, w / 2, h)
  const d = `M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y}`
    + ` L${x + w - r},${y} Q${x + w},${y} ${x + w},${y + r}`
    + ` L${x + w},${y + h} Z`
  return <path d={d} fill={fill} opacity={opacity} />
}

/**
 * Overall score: a single row, in descending order.
 * Single series, so no legend is needed: the heading says it all.
 */
export function OverallChart({ items, highlight }) {
  useLocaleRefresh()
  const [hover, setHover] = useState(null)
  const t = useT()
  if (!items?.length) return null

  const W = 760, H = 300
  const pad = { top: 28, right: 12, bottom: 64, left: 40 }
  const innerW = W - pad.left - pad.right
  const innerH = H - pad.top - pad.bottom
  const sorted = [...items].sort((a, b) => b.ratio - a.ratio)
  const slot = innerW / sorted.length
  const bar = Math.min(BAR_MAX * 1.6, slot - GAP * 2)

  return (
    <figure className="viz" style={{ margin: 0 }}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
           aria-label={t('bench.overallAria')}>
        {/* Gridlines, thin enough to stay unobtrusive */}
        {[0, 25, 50, 75, 100].map((v) => {
          const y = pad.top + innerH - (v / 100) * innerH
          return (
            <g key={v}>
              <line x1={pad.left} y1={y} x2={W - pad.right} y2={y}
                    stroke="var(--border)" strokeWidth="1" />
              <text x={pad.left - 8} y={y + 4} textAnchor="end"
                    fontSize="11" fill="var(--fg-subtle)">{tr(v)}</text>
            </g>
          )
        })}

        {sorted.map((m, i) => {
          const h = Math.max(2, (m.ratio) * innerH)
          const x = pad.left + i * slot + (slot - bar) / 2
          const y = pad.top + innerH - h
          const isHighlighted = m.id === highlight
          const isHovered = hover === m.id
          return (
            <g key={m.id}
               onMouseEnter={() => setHover(m.id)}
               onMouseLeave={() => setHover(null)}>
              {/* Enlarged hit area, so hover works even if the pointer misses the bar */}
              <rect x={pad.left + i * slot} y={pad.top} width={slot} height={innerH}
                    fill="transparent" />
              <Bar x={x} y={y} w={bar} h={h}
                     fill={isHighlighted ? 'var(--accent)' : 'var(--viz-1)'}
                     opacity={isHovered ? 1 : (isHighlighted ? 1 : 0.88)} />
              {/* Value inside the bar, as in the mockup */}
              <text x={x + bar / 2} y={y + 20} textAnchor="middle"
                    fontSize="14" fontWeight="600" fill="#fff">
                {Math.round(m.ratio * 100)}
              </text>
              <text x={x + bar / 2} y={H - pad.bottom + 18} textAnchor="middle"
                    fontSize="11" fill="var(--fg-muted)">{tr(m.id)}</text>
              <text x={x + bar / 2} y={H - pad.bottom + 32} textAnchor="middle"
                    fontSize="10" fill="var(--fg-subtle)">
                {tr(m.correct)}/{tr(m.total)}
              </text>
            </g>
          )
        })}
      </svg>
      <figcaption className="muted small" style={{ marginTop: 6 }}>
        {t('bench.overallCaption')}
      </figcaption>
    </figure>
  )
}

/**
 * Per-category comparison: grouped bars.
 * More than two series, so a legend is required.
 */
export function CategoryChart({ items, categories, highlight }) {
  useLocaleRefresh()
  const [hover, setHover] = useState(null)
  const t = useT()
  if (!items?.length || !categories?.length) return null

  const W = 860, H = 340
  const pad = { top: 24, right: 12, bottom: 96, left: 40 }
  const innerW = W - pad.left - pad.right
  const innerH = H - pad.top - pad.bottom
  const groupW = innerW / items.length
  const bar = Math.min(BAR_MAX, (groupW - 16) / categories.length - GAP)

  return (
    <figure className="viz" style={{ margin: 0 }}>
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img"
           aria-label={t('bench.categoryAria')}>
        {[0, 50, 100].map((v) => {
          const y = pad.top + innerH - (v / 100) * innerH
          return (
            <g key={v}>
              <line x1={pad.left} y1={y} x2={W - pad.right} y2={y}
                    stroke="var(--border)" strokeWidth="1" />
              <text x={pad.left - 8} y={y + 4} textAnchor="end"
                    fontSize="11" fill="var(--fg-subtle)">{tr(v)}</text>
            </g>
          )
        })}

        {items.map((m, gi) => (
          <g key={m.id}>
            {categories.map((k, ci) => {
              const sc = m.scores?.[k]
              if (!sc) return null
              const h = Math.max(2, sc.ratio * innerH)
              const x = pad.left + gi * groupW + 8 + ci * (bar + GAP)
              const y = pad.top + innerH - h
              const dimmed = hover && hover !== k
              return (
                <g key={k}
                   onMouseEnter={() => setHover(k)}
                   onMouseLeave={() => setHover(null)}>
                  <Bar x={x} y={y} w={bar} h={h}
                         fill={`var(--viz-${ci + 1})`}
                         opacity={dimmed ? 0.25 : 1} />
                  <title>{`${m.id} · ${k}: ${sc.correct}/${sc.total}`}</title>
                </g>
              )
            })}
            <text x={pad.left + gi * groupW + groupW / 2} y={H - pad.bottom + 18}
                  textAnchor="middle" fontSize="11"
                  fill={m.id === highlight ? 'var(--fg)' : 'var(--fg-muted)'}
                  fontWeight={m.id === highlight ? 600 : 400}>
              {tr(m.id)}
            </text>
          </g>
        ))}

        {/* Legend, required for more than two series */}
        {categories.map((k, ci) => {
          const col = ci % 3
          const row = Math.floor(ci / 3)
          const x = pad.left + col * 250
          const y = H - 42 + row * 18
          return (
            <g key={k} style={{ cursor: 'default' }}
               onMouseEnter={() => setHover(k)}
               onMouseLeave={() => setHover(null)}>
              <rect x={x} y={y - 8} width="10" height="10" rx="2"
                    fill={`var(--viz-${ci + 1})`}
                    opacity={hover && hover !== k ? 0.3 : 1} />
              <text x={x + 16} y={y + 1} fontSize="11" fill="var(--fg-muted)">{tr(k)}</text>
            </g>
          )
        })}
      </svg>
      <figcaption className="muted small" style={{ marginTop: 6 }}>
        {t('bench.categoryCaption')}
      </figcaption>
    </figure>
  )
}

export { SERIES }
