import { fmtDateTime } from '../format.js'

const TEXT = { up: 'ishlagan', tunnel_down: 'kompyuter yoki internet yo\'q', ollama_down: 'Ollama javob bermagan', none: 'ma\'lumot yo\'q' }

/** 24-hour status strip: each segment is 10 minutes. */
export default function Timeline({ items }) {
  return (
    <div>
      <div className="timeline" role="img" aria-label="Oxirgi 24 soat holati">
        {items.map((b) => <span key={b.t} className={`tl tl--${b.s}`} title={`${fmtDateTime(b.t)}: ${TEXT[b.s]}`} />)}
      </div>
      <div className="legend">
        <span><i className="tl tl--up" /> Ishlagan</span>
        <span><i className="tl tl--tunnel_down" /> Kompyuter yoki internet yo'q</span>
        <span><i className="tl tl--ollama_down" /> Ollama javob bermagan</span>
        <span><i className="tl tl--none" /> Ma'lumot yo'q</span>
      </div>
    </div>
  )
}
