import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { api } from '../api.js'
import { Loading, useLoad } from './ui.jsx'
import { fmtInt, fmtSom } from '../format.js'
import { useT } from '../i18n/index.jsx'

/**
 * Usage by model: each model's token share (horizontal bar) with exact values
 * next to it — tokens, requests, som. One measure (tokens) in one color, largest
 * first; values are in text color, the bar only shows the proportion.
 */
export default function ModelBreakdown({ days = 30, freeMode = false }) {
  useLocaleRefresh()
  const t = useT()
  const { data } = useLoad(() => api.get(`/portal/usage/models?days=${days}`), [days], { refreshMs: 15_000 })
  if (!data) return <Loading rows={2} />
  const items = data.items || []
  if (!items.length) return <p className="muted small" style={{ margin: 0 }}>{t('breakdown.empty')}</p>
  const tok = (m) => (m.prompt_tokens || 0) + (m.completion_tokens || 0)
  const max = Math.max(1, ...items.map(tok))
  const total = items.reduce((s, m) => s + tok(m), 0) || 1
  return (
    <div className={`model-break${freeMode ? ' model-break--free' : ''}`} role="table" aria-label={t('breakdown.aria')}>
      <div className="model-break__row model-break__head" role="row">
        <span role="columnheader">{t('breakdown.model')}</span>
        <span role="columnheader" className="model-break__bar-col">{t('breakdown.share')}</span>
        <span role="columnheader" className="num">{t('breakdown.tokens')}</span>
        <span role="columnheader" className="num">{t('breakdown.requests')}</span>
        {!freeMode && <span role="columnheader" className="num">{t('breakdown.cost')}</span>}
      </div>
      {items.map((m) => {
        const n = tok(m)
        const share = Math.round((n / total) * 100)
        return (
          <div className="model-break__row" role="row" key={m.model}
               title={`${m.model}: ${t('breakdown.tokensInOut', { in: fmtInt(m.prompt_tokens), out: fmtInt(m.completion_tokens) })}`}>
            <span role="cell"><span className="tag tag--sky tag--mono">{tr(m.model)}</span></span>
            <span role="cell" className="model-break__bar-col">
              <span className="model-break__track"><span className="model-break__bar" style={{ width: `${Math.max(2, (n / max) * 100)}%` }} /></span>
              <span className="model-break__pct">{tr(share)}%</span>
            </span>
            <span role="cell" className="num">{fmtInt(n)}</span>
            <span role="cell" className="num">{fmtInt(m.requests)}</span>
            {!freeMode && <span role="cell" className="num">{fmtSom(m.cost)}</span>}
          </div>
        )
      })}
    </div>
  )
}
