import { Link, useParams } from 'react-router-dom'
import { api } from '../api.js'
import { useAuth } from '../auth.js'
import { CategoryChart, OverallChart } from '../components/BenchmarkChart.jsx'
import { ErrorNote, Loading, useLoad } from '../components/ui.jsx'
import { MICRO, fmtNumber } from '../format.js'

// Model capabilities, in the user's language, without internal jargon
const CAPS = [
  ['vision', 'Rasmni ko\'radi', 'Rasm yuborib, undagi narsalarni tahlil qildirish mumkin.'],
  ['file_input', 'Fayl o\'qiydi', 'PDF, matn va jadval fayllarini yuborib tahlil qildirish mumkin.'],
  ['image_generation', 'Rasm yaratadi', 'Tavsif bo\'yicha yangi rasm chizadi.'],
  ['tool_calling', 'Agent rejimi', 'Dasturingizdagi funksiyalarni o\'zi chaqira oladi.'],
]

// Typical request: 300 input + 500 output tokens
const SAMPLE_IN = 300
const SAMPLE_OUT = 500

function sampleCost(m) {
  return (SAMPLE_IN * m.price_in_per_1m + SAMPLE_OUT * m.price_out_per_1m) / 1_000_000 / MICRO
}

/** Bar comparison: the larger the value, the longer the bar. */
function CompareBars({ title, note, items, current, value, format, footnote }) {
  const withValue = items.filter((x) => value(x) != null)
  if (withValue.length < 2) return null
  const max = Math.max(...withValue.map(value))
  return (
    <div className="panel" style={{ marginTop: 14 }}>
      <h3 style={{ marginTop: 0, fontSize: '1rem' }}>{title}</h3>
      {note && <p className="muted small" style={{ marginTop: 4 }}>{note}</p>}
      <div style={{ display: 'grid', gap: 8, marginTop: 12 }}>
        {withValue.map((x) => {
          const v = value(x)
          const share = max > 0 ? Math.max(0.04, v / max) : 0
          const isCurrent = x.id === current
          return (
            <div key={x.id} style={{ display: 'grid', gridTemplateColumns: '7rem 1fr auto', alignItems: 'center', gap: 10 }}>
              <code className="small" style={{ opacity: isCurrent ? 1 : 0.6 }}>{x.id}</code>
              <div style={{ height: 8, borderRadius: 999, background: 'var(--border)', overflow: 'hidden' }}>
                <div style={{
                  width: `${share * 100}%`, height: '100%', borderRadius: 999,
                  background: isCurrent ? 'var(--accent)' : 'var(--muted)',
                  opacity: isCurrent ? 1 : 0.45,
                }} />
              </div>
              <span className="small nowrap" style={{ opacity: isCurrent ? 1 : 0.6 }}>{format(v)}</span>
            </div>
          )
        })}
      </div>
      {footnote && <p className="muted small" style={{ marginTop: 10 }}>{footnote}</p>}
    </div>
  )
}

export default function ModelDetail() {
  const { id } = useParams()
  const { freeMode } = useAuth()
  const { data, error, loading, reload } = useLoad(() => api.get('/portal/models'), [])
  const bench = useLoad(() => api.get('/portal/benchmark'), [])
  const list = data?.items || []
  const m = list.find((x) => x.id === id)

  if (loading && !data) return <Loading rows={4} />
  if (error) return <ErrorNote error={error} retry={reload} />
  if (!m) {
    return (
      <>
        <div className="page-head"><div><h1>Model topilmadi</h1></div></div>
        <p className="muted">
          <code>{id}</code> degan model ro'yxatda yo'q. <Link to="/app/catalog">Modellar ro'yxatiga qaytish</Link>
        </p>
      </>
    )
  }

  const caps = CAPS.filter(([k]) => m.capabilities?.[k])
  const measured = m.measured

  return (
    <>
      <div className="page-head">
        <div>
          <Link to="/app/catalog" className="dim small">&larr; Modellar</Link>
          <h1 style={{ marginTop: 6 }}>{m.display_name}</h1>
          <p><code>{m.id}</code> &middot; {m.description}</p>
        </div>
      </div>

      {m.maintenance && (
        <div className="panel" style={{ borderColor: 'var(--warn)', marginBottom: 14 }}>
          <b className="small">Bu modelda texnik ishlar ketyapti</b>
          <p className="muted small" style={{ margin: '4px 0 0' }}>
            Model ishlaydi, lekin hozir ba'zi so'rovlar xato qaytarishi mumkin.
            Uzluksiz ish uchun vaqtincha boshqa modelni tanlang.
          </p>
        </div>
      )}

      <div className="metric-grid">
        <div className="metric">
          <span className="label">Kontekst oynasi</span>
          <b>{fmtNumber(m.context_window)}</b><span className="trend">token</span>
        </div>
        <div className="metric">
          <span className="label">Javob chegarasi</span>
          <b>{fmtNumber(m.max_output_tokens)}</b><span className="trend">token</span>
        </div>
        {!freeMode && (
          <div className="metric">
            <span className="label">O'rtacha so'rov</span>
            <b>{sampleCost(m).toFixed(1)} so'm</b>
            <span className="trend">{SAMPLE_IN} kirish + {SAMPLE_OUT} chiqish</span>
          </div>
        )}
        {measured && (
          <div className="metric">
            <span className="label">Odatdagi javob vaqti</span>
            <b>{(measured.typical_latency_ms / 1000).toFixed(1)} s</b>
            <span className="trend">so'nggi {measured.samples} ta so'rov medianasi</span>
          </div>
        )}
      </div>

      <div className="block">
        <div className="block-head"><h2>Nimalarni qila oladi</h2></div>
        {caps.length === 0 ? (
          <p className="muted small">Bu model matn bilan ishlaydi: savol-javob, tarjima, matn yozish va tahlil.</p>
        ) : (
          <div style={{ display: 'grid', gap: 10 }}>
            {caps.map(([k, label, desc]) => (
              <div className="panel" key={k} style={{ padding: '12px 14px' }}>
                <b className="small">{label}</b>
                <p className="muted small" style={{ margin: '4px 0 0' }}>{desc}</p>
              </div>
            ))}
          </div>
        )}
        {m.capabilities?.reasoning_levels?.length > 0 && (
          <p className="muted small" style={{ marginTop: 12 }}>
            Fikrlash rejimi: javob berishdan oldin o'ylaydi. Darajalar:{' '}
            {m.capabilities.reasoning_levels.join(', ')}.
          </p>
        )}
      </div>

      {(() => {
        const benchItems = bench.data?.items || []
        const own = benchItems.find((x) => x.id === m.id)
        if (!own) return null
        const categories = bench.data?.categories || []
        return (
          <div className="block">
            <div className="block-head">
              <h2>Qaysi ishlarda kuchli</h2>
              <span className="dim small">
                o'z sinovlarimiz · {own.correct}/{own.total} to'g'ri
              </span>
            </div>
            <p className="muted small" style={{ marginTop: 0 }}>
              Har bir yo'nalish bo'yicha bir xil topshiriqlar barcha modellarga
              berildi va javoblar avtomatik tekshirildi. Raqamlar shu sinovdan,
              tashqi manbalardan emas.
            </p>
            <div className="panel" style={{ marginTop: 12 }}>
              <CategoryChart items={benchItems} categories={categories} highlight={m.id} />
            </div>

            <div className="panel" style={{ marginTop: 12 }}>
              <h3 style={{ marginTop: 0, fontSize: '.95rem' }}>Umumiy natija</h3>
              <OverallChart items={benchItems} highlight={m.id} />
              {bench.data?.updated_at && (
                <p className="muted small" style={{ marginTop: 6, marginBottom: 0 }}>
                  Oxirgi sinov: {new Date(bench.data.updated_at * 1000).toLocaleDateString('uz-UZ')}
                </p>
              )}
            </div>
          </div>
        )
      })()}

      <div className="block">
        <div className="block-head">
          <h2>Boshqa modellar bilan taqqoslash</h2>
          <span className="dim small">barcha ko'rsatkichlar haqiqiy o'lchovdan</span>
        </div>

        <CompareBars
          title="Javob tezligi"
          note="So'nggi so'rovlarning medianasi: bitta tasodifiy kechikish ko'rsatkichni buzmaydi. Qisqa ustun — tezroq."
          items={list.filter((x) => x.measured)}
          current={m.id}
          value={(x) => x.measured?.typical_latency_ms}
          format={(v) => `${(v / 1000).toFixed(1)} s`}
          footnote="Bu yerda ustun qancha qisqa bo'lsa, model shuncha tez javob beradi."
        />

        <CompareBars
          title="Kontekst oynasi"
          note="Bitta so'rovda model qancha matnni bir vaqtda ko'ra oladi."
          items={list}
          current={m.id}
          value={(x) => x.context_window}
          format={(v) => `${fmtNumber(v)} token`}
        />

        {!freeMode && (
          <CompareBars
            title="O'rtacha so'rov narxi"
            note={`${SAMPLE_IN} kirish va ${SAMPLE_OUT} chiqish tokeni uchun.`}
            items={list}
            current={m.id}
            value={sampleCost}
            format={(v) => `${v.toFixed(1)} so'm`}
          />
        )}
      </div>

      {m.data_processing === 'third_party' && (
        <div className="block">
          <div className="block-head"><h2>Maxfiylik</h2></div>
          <div className="panel">
            <p className="muted small" style={{ margin: 0 }}>
              Bu modelda so'rov mazmuni javob tayyorlash uchun hamkor xizmatga ham yuboriladi.
              Matnlar bizda saqlanmaydi, lekin maxfiy ma'lumot yuborishdan oldin shuni hisobga oling.
              Faqat o'z serverimizda ishlaydigan modellar uchun bu yozuv ko'rinmaydi.
            </p>
          </div>
        </div>
      )}

      <div className="block">
        <div className="block-head"><h2>Qanday ishlatish</h2></div>
        <div className="panel">
          <pre className="code" style={{ margin: 0 }}>{`client.chat.completions.create(
    model="${m.id}",
    messages=[{"role": "user", "content": "Salom!"}],
)`}</pre>
        </div>
        <p className="muted small" style={{ marginTop: 10 }}>
          To'liq misollar <Link to="/app/docs">Hujjatlar</Link> sahifasida.
        </p>
      </div>
    </>
  )
}
