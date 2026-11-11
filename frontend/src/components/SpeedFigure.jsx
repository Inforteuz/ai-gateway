import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'

/**
 * A model's typical response time (median of recent requests) with its trend
 * against the window before: slower in red with ▼, faster in green with ▲, and
 * a word under it, so the meaning never rests on color alone. Within ±15% it
 * reads as steady (request sizes vary, smaller swings are noise).
 */
const LOOK = {
  slower: { cls: 'speed speed--slower', mark: '▼', label: 'sekinlashdi' },
  faster: { cls: 'speed speed--faster', mark: '▲', label: 'tezlashdi' },
}

export default function SpeedFigure({ measured, sub = true }) {
  useLocaleRefresh()
  if (!measured) return <><b>—</b>{sub && <span className="sub">{tr("(o'rtacha)")}</span>}</>
  const look = LOOK[measured.trend]
  const secs = (measured.typical_latency_ms / 1000).toFixed(1)
  const before = measured.previous_latency_ms ? (measured.previous_latency_ms / 1000).toFixed(1) : null
  return (
    <>
      <b className={look ? look.cls : 'speed'} title={before ? `${tr('Oldin')}: ${before} s` : undefined}>
        {secs} s{look && <span className="speed__mark" aria-hidden="true"> {look.mark}</span>}
      </b>
      {sub && <span className={`sub ${look ? look.cls : ''}`}>{tr(look ? look.label : "(o'rtacha)")}</span>}
    </>
  )
}
