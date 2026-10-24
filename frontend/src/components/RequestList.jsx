import { translateText as tr } from '../i18n/index.jsx'
import { fmtDateTime, fmtInt } from '../format.js'
import { IconChat } from './icons.jsx'
import { useT } from '../i18n/index.jsx'

/** "Recent requests" list from the mockup: one row per request. */
export default function RequestList({ items, empty }) {
  const t = useT()
  if (!items.length) return <div className="req-empty">{tr(empty || t('req.empty'))}</div>
  return (
    <div className="req-list">
      {items.map((r) => {
        const failed = r.status_code >= 400
        // The first model was down and the stand-in answered: the request did
        // succeed, so it must not read as a plain failure.
        const served = failed && r.retried_on
        return (
          <div key={r.id} className="req-row">
            <span className={`ico-chip ico-chip--sm ${served ? 'ico-chip--amber' : failed ? 'ico-chip--rose' : ''}`}><IconChat /></span>
            <span className="req-main">
              <span className="req-top"><span className="req-time">{fmtDateTime(r.created_at)}</span></span>
              <b>{tr(failed ? t('req.error', { code: r.status_code }) : r.model)}</b>
              <span className="req-meta">
                {served
                  ? t('req.servedByStandIn', { model: r.retried_on })
                  : tr(failed
                    ? t('req.failedNote', { reason: r.reason || r.error || t('req.failed') })
                    : t('req.tokensInOut', { in: fmtInt(r.prompt_tokens), out: fmtInt(r.completion_tokens) }))}
              </span>
            </span>
            <span className={`tag ${served ? 'tag--warn' : failed ? 'tag--bad' : 'tag--ok'}`}>
              <i className="req-dot" aria-hidden="true" />
              {served ? t('req.standIn') : tr(failed ? t('req.errorShort') : t('req.success'))}
            </span>
          </div>
        )
      })}
    </div>
  )
}
