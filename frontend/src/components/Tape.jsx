import { fmtDateTime, fmtInt, fmtSom } from '../format.js'
import { LEVEL_SHORT } from '../reasoning.jsx'

/** Request "receipt tape": each row is one request and its cost. */
export default function Tape({ items, showUser = false, empty }) {
  if (!items.length) return <div className="tape"><div className="tape-empty">{empty || 'Hozircha so\'rov yo\'q.'}</div></div>
  const total = items.reduce((s, r) => s + r.cost, 0)
  return (
    <div className="tape">
      {items.map((r) => {
        const failed = r.status_code >= 400
        return (
          <div key={r.id} className={`tape-row ${failed ? 'tape-row--fail' : ''}`}>
            <span className="tape-time">{fmtDateTime(r.created_at)}</span>
            <span className="tape-main">
              <b>{failed ? `Xato ${r.status_code}` : r.model}</b>
              <span className="tape-meta">
                {failed
                  ? (r.reason || r.error || 'so\'rov bajarilmadi') + ', pul yechilmadi'
                  : `${fmtInt(r.prompt_tokens)} kirish + ${fmtInt(r.completion_tokens)} chiqish${r.estimated ? ' (taxminiy)' : ''}${r.reasoning ? `, fikrlash: ${LEVEL_SHORT[r.reasoning] || r.reasoning}` : ''}`}
                {r.key_name ? `, ${r.key_name}` : ''}{showUser && r.email ? `, ${r.email}` : ''}
                {!failed && r.latency_ms ? `, ${(r.latency_ms / 1000).toFixed(1)} s` : ''}
              </span>
            </span>
            <span className={`tape-cost ${r.cost === 0 ? 'tape-cost--zero' : ''}`}>{r.cost === 0 ? (failed ? '0' : 'bepul') : fmtSom(-r.cost, { sign: true })}</span>
          </div>
        )
      })}
      <div className="tape-total"><span>Shu sahifada jami</span><span>{fmtSom(total)}</span></div>
    </div>
  )
}
