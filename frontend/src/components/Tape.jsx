import { translate, translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { fmtDateTime, fmtInt, fmtSom } from '../format.js'
import { levelShort } from '../reasoning.jsx'

/** Request "receipt tape": each row is one request and its cost. */
export default function Tape({ items, showUser = false, empty }) {
  useLocaleRefresh()
  if (!items.length) return <div className="tape"><div className="tape-empty">{tr(empty || tr("Hozircha so'rov yo'q."))}</div></div>
  const total = items.reduce((s, r) => s + r.cost, 0)
  return (
    <div className="tape">
      {items.map((r) => {
        const failed = r.status_code >= 400
        return (
          <div key={r.id} className={`tape-row ${failed ? 'tape-row--fail' : ''}`}>
            <span className="tape-time">{fmtDateTime(r.created_at)}</span>
            <span className="tape-main">
              <b>{tr(failed ? tr("Xato {v0}", { v0: (r.status_code) }) : r.model)}</b>
              <span className="tape-meta">
                {/* Each piece is translated on its own. A sentence glued together
                    ("<reason>, pul yechilmadi", "<n> kirish + <m> chiqish") can never
                    be a dictionary key, so it stayed Uzbek in every language. */}
                {failed
                  ? <>{tr(r.reason || r.error || "so'rov bajarilmadi")}, {tr("pul yechilmadi")}</>
                  : <>
                      {translate('req.tokensInOut', { in: fmtInt(r.prompt_tokens), out: fmtInt(r.completion_tokens) })}
                      {r.estimated ? ` ${tr("(taxminiy)")}` : ''}
                      {r.reasoning ? `, ${tr("fikrlash")}: ${levelShort(translate, r.reasoning)}` : ''}
                    </>}
                {r.key_name ? `, ${r.key_name}` : ''}{showUser && r.email ? `, ${r.email}` : ''}
                {!failed && r.latency_ms ? `, ${(r.latency_ms / 1000).toFixed(1)} s` : ''}
              </span>
            </span>
            <span className={`tape-cost ${r.cost === 0 ? 'tape-cost--zero' : ''}`}>{tr(r.cost === 0 ? (failed ? '0' : tr("bepul")) : fmtSom(-r.cost, { sign: true }))}</span>
          </div>
        )
      })}
      <div className="tape-total"><span>{tr("Shu sahifada jami")}</span><span>{fmtSom(total)}</span></div>
    </div>
  )
}
