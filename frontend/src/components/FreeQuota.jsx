import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { IconLayers } from './icons.jsx'
import { fmtInt } from '../format.js'
import { useT } from '../i18n/index.jsx'

/** Account without a deposit: today's token use on the free model. Renders nothing when `quota` is null (no limit). */
export default function FreeQuota({ quota, style }) {
  useLocaleRefresh()
  const t = useT()
  if (!quota) return null
  const used = Math.min(100, Math.round((quota.used * 100) / quota.limit))
  return (
    <div className="mini-card" style={style}>
      <span className="mini-card__head"><span className="ico-chip ico-chip--sm"><IconLayers /></span> {t('quota.today', { models: quota.models.join(', ') })}</span>
      <div className="meter">
        <b className="mini-card__fig">{t('quota.tokens', { used: fmtInt(quota.used), limit: fmtInt(quota.limit) })}</b>
        <span className="track"><span className="fill" style={{ width: `${used}%` }} /></span>
        <span className="ends"><span>{t('quota.left', { n: fmtInt(quota.remaining) })}</span><span>{tr(used)}%</span></span>
      </div>
      <span className="dim small">
        {t('quota.note')}
      </span>
    </div>
  )
}
