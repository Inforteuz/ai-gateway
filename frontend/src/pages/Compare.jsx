import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { api } from '../api.js'
import { useAuth } from '../auth.js'
import { Loading, useLoad } from '../components/ui.jsx'
import { CategoryChart } from '../components/BenchmarkChart.jsx'
import PriceScoreChart from '../components/PriceScoreChart.jsx'
import { fmtInt, fmtNumber } from '../format.js'
import './Compare.css'

/**
 * Comparison page.
 *
 * THE PAGE'S CORE RULE: our own measurements and the published numbers of
 * external models are NEVER shown in the same chart, table, or column. The
 * benchmarks differ, so the percentages measure different things. Putting
 * them side by side looks nice but would be misleading. That's why the page
 * is split in two, with an explanation of why in between.
 */

const pct = (r) => `${Math.round(r * 100)}%`

/** Finds the best model(s) in each category. */
function categoryLeaders(items, categories) {
  return categories.map((k) => {
    const ranked = items
      .filter((m) => m.scores?.[k])
      .sort((a, b) => (b.scores[k].ratio - a.scores[k].ratio) || (b.ratio - a.ratio))
    if (!ranked.length) return null
    const best = ranked[0].scores[k].ratio
    const tied = ranked.filter((m) => m.scores[k].ratio === best)
    return {
      category: k,
      model: ranked[0],
      sc: ranked[0].scores[k],
      ties: tied.length - 1,   // number of other models that matched this score
    }
  }).filter(Boolean)
}

/** Formats a number as in the source: integers as-is, fractions with one decimal. */
const fmtScore = (v) => (v == null ? '—' : Number.isInteger(v) ? fmtInt(v) : fmtNumber(v, 1))

/** "ranking": models listed by a single metric. */
function RankingTable({ s }) {
  useLocaleRefresh()
  return (
    <table className="table cmp-table">
      <thead>
        <tr>
          <th>{tr("Model")}</th>
          <th>{tr("Ishlab chiqaruvchi")}</th>
          <th className="num">{tr("Natija")}</th>
        </tr>
      </thead>
      <tbody>
        {s.rows.map((r) => (
          <tr key={`${r.vendor}-${r.model}`}>
            <td>{tr(r.model)}</td>
            <td className="muted">{tr(r.vendor)}</td>
            <td className="num">{fmtScore(r.score)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/** "model": tests from a single model's own announcement. */
function ModelTable({ s }) {
  useLocaleRefresh()
  return (
    <table className="table cmp-table">
      <thead>
        <tr>
          <th>{tr("Test")}</th>
          <th className="num">{tr(s.model)}</th>
        </tr>
      </thead>
      <tbody>
        {s.rows.map((r, i) => (
          <tr key={`${r.test}-${i}`}>
            <td>
              {tr(r.test)}
              {r.note && <span className="cmp-note"> — {tr(r.note)}</span>}
            </td>
            <td className="num">
              {fmtScore(r.score)}{tr(r.unit ? ` ${r.unit}` : '%')}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

/**
 * "matrix": several models across several tests in one document.
 *
 * The top value is deliberately NOT highlighted: the document itself says
 * that numbers for models other than Gemini are taken from figures published
 * by those vendors, so marking a "winner" would lead readers to assume the
 * numbers were measured under the same conditions.
 */
function MatrixTable({ s }) {
  useLocaleRefresh()
  return (
    <table className="table cmp-table cmp-matrix">
      <thead>
        <tr>
          <th>{tr("Test")}</th>
          {s.columns.map((c, i) => (
            <th key={c} className="num">
              {tr(c)}
              {s.column_vendors?.[i] && (
                <span className="cmp-note"> {tr(s.column_vendors[i])}</span>
              )}
            </th>
          ))}
        </tr>
      </thead>
      {s.groups.map((g) => (
        <tbody key={g.group}>
          <tr className="cmp-group">
            <th colSpan={s.columns.length + 1}>{tr(g.group)}</th>
          </tr>
          {g.rows.map((r, i) => (
            <tr key={`${r.test}-${i}`}>
              <td>
                {tr(r.test)}
                {r.note && <span className="cmp-note"> — {tr(r.note)}</span>}
              </td>
              {r.scores.map((v, j) => (
                <td key={s.columns[j]} className="num">{fmtScore(v)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      ))}
    </table>
  )
}

function ExternalSource({ s }) {
  useLocaleRefresh()
  return (
    <article className="panel cmp-src">
      <header className="cmp-src-head">
        <div>
          <h3>{tr(s.name)}</h3>
          <p className="muted small">{tr(s.version)} · {tr(s.scale)}</p>
        </div>
        <a className="btn btn--small" href={s.source_url} target="_blank" rel="noreferrer noopener"> {tr("Manbani ochish")} </a>
      </header>

      <dl className="cmp-meta">
        <div>
          <dt>{tr("Nimani o'lchaydi")}</dt>
          <dd>{tr(s.what_it_measures)}</dd>
        </div>
        <div>
          <dt>{tr("Nega bizning foizimiz bilan solishtirilmaydi")}</dt>
          <dd>{tr(s.why_not_comparable)}</dd>
        </div>
        <div>
          <dt>{tr("Sana")}</dt>
          <dd>{tr(s.read_date)}{tr(s.date_note ? ` (${s.date_note})` : '')}</dd>
        </div>
      </dl>

      <div className="table-wrap">
        {s.kind === 'matrix' ? <MatrixTable s={s} />
          : s.kind === 'model' ? <ModelTable s={s} />
            : <RankingTable s={s} />}
      </div>
      <p className="muted small" style={{ marginTop: 10 }}> {tr("Qatorlar manbadagi nom va tartibda keltirilgan, hech narsa o'zgartirilmagan. Raqamlar:")} <a href={s.source_url} target="_blank"
        rel="noreferrer noopener">{tr(new URL(s.source_url).hostname)}</a>, {tr(s.read_date)}.
      </p>
    </article>
  )
}

export default function Compare() {
  useLocaleRefresh()
  const { freeMode } = useAuth()
  const bench = useLoad(() => api.get('/portal/benchmark'), [])
  const models = useLoad(() => api.get('/portal/models'), [])
  const external = useLoad(() => api.get('/portal/benchmark/external'), [])

  // Models not in the catalog should not appear in the charts.
  const catalog = models.data?.items?.length
    ? new Map(models.data.items.map((m) => [m.id, m]))
    : null
  const items = (bench.data?.items || []).filter((m) => !catalog || catalog.has(m.id))
  const categories = bench.data?.categories || []
  const leaders = categoryLeaders(items, categories)
  const total = items[0]?.total

  // Price only makes sense when free mode is off.
  const pricePoints = freeMode ? [] : items
    .map((m) => {
      const cm = catalog?.get(m.id)
      if (!cm || cm.price_out_per_1m == null) return null
      return {
        id: m.id, ratio: m.ratio, correct: m.correct, total: m.total,
        price: cm.price_out_per_1m / 1_000_000,
      }
    })
    .filter(Boolean)

  return (
    <div className="wrap cmp" style={{ paddingBottom: 72 }}>
      <section className="cmp-hero">
        <span className="eyebrow">{tr("Taqqoslash")}</span>
        <h1>{tr("Qaysi model qaysi ishda kuchli")}</h1>
        <p className="muted"> {tr("Bu sahifada ikki xil ma'lumot bor va ular ataylab aralashtirilmagan. Birinchisi — bizning o'z sinovimiz: har bir modelga bir xil")}{' '}
          {tr(total ? tr("{v0} ta qiyin topshiriq", { v0: (total) }) : tr("qiyin topshiriqlar"))} {tr("berilgan, javoblar avtomatik tekshirilgan. Ikkinchisi — dunyodagi mashhur modellarning o'z sinovlarida e'lon qilgan natijalari, manbasi va sanasi bilan.")} </p>
      </section>

      {/* --- Part 1: our measurements -------------------------------------- */}
      <section className="cmp-sec">
        <div className="section-head">
          <span className="eyebrow">{tr("Bizning o'lchovimiz")}</span>
          <h2>{tr("Yo'nalishlar bo'yicha")}</h2>
          <p> {tr("Topshiriqlar")} {tr(categories.length || 6)} {tr("yo'nalishga bo'lingan. Har bir yo'nalishda eng yaxshi natija bergan model quyida — narx emas, natija bo'yicha. Arzon model yetarli bo'lsa, qimmatrog'iga pul to'lashning hojati yo'q.")} </p>
        </div>

        {bench.loading && !items.length
          ? <Loading rows={4} />
          : !items.length
            ? <p className="muted">{tr("Sinov natijalari hozir mavjud emas.")}</p>
            : (
              <>
                <ul className="cmp-leaders">
                  {leaders.map((y) => (
                    <li key={y.category} className="cmp-leader">
                      <span className="cmp-leader-cat">{tr(y.category)}</span>
                      <b>{tr(y.model.display_name || y.model.id)}</b>
                      <span className="cmp-leader-score">
                        {pct(y.sc.ratio)} <span className="dim">({tr(y.sc.correct)}/{tr(y.sc.total)})</span>
                      </span>
                      {y.ties > 0 && (
                        <span className="cmp-leader-tie"> {tr("shu natijani yana")} {tr(y.ties)} {tr("ta model takrorladi")} </span>
                      )}
                    </li>
                  ))}
                </ul>

                <div className="panel">
                  <div className="panel-head">
                    <div className="panel-head__t">
                      <h3>{tr("Har bir model — har bir yo'nalishda")}</h3>
                      <p>{tr("Ustun balandligi — to'g'ri bajarilgan topshiriqlar ulushi.")}</p>
                    </div>
                  </div>
                  <CategoryChart items={items} categories={categories} />
                </div>

                {pricePoints.length > 1 && (
                  <div className="panel" style={{ marginTop: 20 }}>
                    <div className="panel-head">
                      <div className="panel-head__t">
                        <h3>{tr("Narx va natija")}</h3>
                        <p>{tr("Bir xil sinov, har bir modelning o'z narxi bilan.")}</p>
                      </div>
                    </div>
                    <PriceScoreChart points={pricePoints} />
                  </div>
                )}

                {bench.data?.updated_at && (
                  <p className="muted small" style={{ marginTop: 12 }}> {tr("Oxirgi sinov:")} {new Date(bench.data.updated_at * 1000)
                      .toLocaleDateString('uz-UZ')}{tr(". Sinov muntazam takrorlanadi, natija o'zgarsa bu sahifa ham o'zgaradi — yaxshi tomonga ham, yomon tomonga ham.")} </p>
                )}
              </>
            )}
      </section>

      {/* --- Middle explanation: this is where the page stays honest ------ */}
      <section className="cmp-sec">
        <div className="cmp-warn">
          <b>{tr("Nega bizning foizimiz va GPT'ning foizi bitta jadvalda emas")}</b>
          <p> {tr("Chunki sinov boshqa. Bizning 30 ta topshiriq o'zbek tilida va kundalik ishlarga qaratilgan. Dunyo modellari e'lon qiladigan raqamlar esa boshqa sinovlarda olingan: ingliz tilidagi ilmiy savollar, terminal topshiriqlari, agentlik vazifalari. Ikki xil sinovning foizini yonma-yon qo'yish — bir sinovda 5 baho, boshqa sinovda 5 baho olgan ikki o'quvchini \"teng\" deb e'lon qilish bilan bir xil.")} </p>
          <p> {tr("Shuning uchun quyidagi raqamlar alohida turadi va har birining yonida sinov nomi, manba havolasi va sana yozilgan. Bizning raqamlarimizni ularning ustiga chizmaymiz. Siz havolani ochib o'zingiz tekshirib ko'rishingiz mumkin — biz aynan shuni xohlaymiz.")} </p>
        </div>
      </section>

      {/* --- Part 2: external sources -------------------------------------- */}
      <section className="cmp-sec">
        <div className="section-head">
          <span className="eyebrow">{tr("Tashqi manbalar")}</span>
          <h2>{tr("Dunyo modellari nima e'lon qilgan")}</h2>
          {external.data?.disclaimer && <p>{tr(external.data.disclaimer)}</p>}
        </div>

        {external.loading && !external.data
          ? <Loading rows={4} />
          : !external.data?.sources?.length
            ? <p className="muted">{tr("Tashqi ma'lumot hozir mavjud emas.")}</p>
            : (
              <div className="cmp-srcs">
                {external.data.sources.map((s) => <ExternalSource key={s.key} s={s} />)}
              </div>
            )}

        <p className="muted small" style={{ marginTop: 18 }}> {tr("Raqamlar faqat birlamchi manbadan olinadi: o'lchovni o'tkazgan tashkilotning o'z sahifasi yoki ilmiy maqolaning o'zi. Blog va \"eng yaxshi 10 model\" ro'yxatlaridan raqam ko'chirilmaydi — tekshirganimizda bir xil model va bir xil sinov uchun turli bloglarda bir-biriga mos kelmaydigan raqamlar chiqdi.")} </p>
      </section>
    </div>
  )
}
