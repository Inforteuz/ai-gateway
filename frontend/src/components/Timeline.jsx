import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { fmtDateTime } from '../format.js'

const TEXT = { up: 'ishlagan', tunnel_down: 'kompyuter yoki internet yo\'q', ollama_down: 'Model xizmati javob bermagan', none: 'ma\'lumot yo\'q' }

/** 24-hour status strip: each segment is 10 minutes. */
export default function Timeline({ items }) {
  useLocaleRefresh()
  return (
    <div>
      <div className="timeline" role="img" aria-label={tr("Oxirgi 24 soat holati")}>
        {items.map((b) => <span key={b.t} className={`tl tl--${b.s}`} title={`${fmtDateTime(b.t)}: ${TEXT[b.s]}`} />)}
      </div>
      <div className="legend">
        <span><i className="tl tl--up" /> {tr("Ishlagan")}</span>
        <span><i className="tl tl--tunnel_down" /> {tr("Kompyuter yoki internet yo'q")}</span>
        <span><i className="tl tl--ollama_down" /> {tr("Model xizmati javob bermagan")}</span>
        <span><i className="tl tl--none" /> {tr("Ma'lumot yo'q")}</span>
      </div>
    </div>
  )
}
