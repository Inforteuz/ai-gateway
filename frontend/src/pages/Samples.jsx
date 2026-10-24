import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { api } from '../api.js'
import { Empty, Loading, useLoad } from '../components/ui.jsx'
import { Markdown } from '../components/Markdown.jsx'
import { fmtDateTime } from '../format.js'

function Answer({ title, tag, ms, children }) {
  useLocaleRefresh()
  return (
    <div className="sample-col">
      <div className="sample-col-head">
        <b>{tr(title)}</b>
        {tr(tag)}
        {ms != null && <span className="dim small">{(ms / 1000).toFixed(1)} {tr("s")}</span>}
      </div>
      <div className="sample-text">
        {children ? <Markdown>{children}</Markdown> : <span className="dim">{tr("(bo'sh javob)")}</span>}
      </div>
    </div>
  )
}

export default function Samples() {
  useLocaleRefresh()
  const { data, loading } = useLoad(() => api.get('/portal/samples'), [])
  const items = data?.items || []
  return (
    <div className="wrap" style={{ paddingBottom: 64 }}>
      <section className="status-hero">
        <h1>{tr("Javob namunalari")}</h1>
        <p className="muted" style={{ maxWidth: '40rem' }}> {tr("Quyidagi savollar haqiqiy so'rov sifatida yuborilgan, javoblar tahrir qilinmagan. Chapda oddiy rejim, o'ngda javob ikkinchi model tomonidan tekshirilgan rejim. Natija har safar biroz farq qilishi mumkin.")} </p>
      </section>
      {loading && !data && <Loading rows={4} />}
      {data && items.length === 0 && <Empty title={tr("Namunalar hali tayyor emas")}>{tr("Tez orada qo'shiladi.")}</Empty>}
      {items.map((s, i) => (
        <section className="panel sample" key={i} style={{ marginTop: 16, borderTop: '1px solid var(--border)' }}>
          <div className="sample-q"><span className="tag">{tr(s.category)}</span><p>{tr(s.prompt)}</p></div>
          <div className="sample-cols">
            <Answer title={tr("Oddiy rejim")} ms={s.fast.ms}>{s.fast.answer.trim()}</Answer>
            <Answer title={tr("Tekshiruvli rejim")} ms={s.pro.ms}
              tag={s.pro.verified ? <span className="tag tag--ok">{tr("tekshirildi")}</span> : <span className="tag tag--warn">{tr("tekshiruv bajarilmadi")}</span>}>
              {s.pro.answer.trim()}
            </Answer>
          </div>
        </section>
      ))}
      {data?.generated_at && <p className="dim small" style={{ marginTop: 28 }}>{tr("Namunalar yaratilgan:")} {fmtDateTime(data.generated_at)}</p>}
    </div>
  )
}
