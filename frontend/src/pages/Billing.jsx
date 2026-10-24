import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import FreeQuota from '../components/FreeQuota.jsx'
import { useAuth } from '../auth.js'
import { Empty, ErrorNote, Loading, PageHead, Pager, useLoad, useToast } from '../components/ui.jsx'
import {
  IconArrowRight, IconChart, IconClock, IconHeadset, IconSend, IconShield, IconWallet, IconZap,
} from '../components/icons.jsx'
import { fmtDateTime, fmtNumber, fmtSom, somToMicro } from '../format.js'

const KIND = { topup: 'To\'ldirish', usage: 'So\'rov', adjustment: 'Tuzatish', refund: 'Qaytarish', bonus: 'Sovg\'a', referral: 'Taklif mukofoti' }
const STATUS = { pending: ['Kutilmoqda', 'tag--warn'], approved: ['Tasdiqlandi', 'tag--ok'], rejected: ['Rad etildi', 'tag--bad'] }
const PRESETS = [50_000, 100_000, 250_000, 500_000]
const LIMIT = 20

export default function Billing() {
  useLocaleRefresh()
  const { freeMode } = useAuth()
  const toast = useToast()
  const sum = useLoad(() => api.get('/portal/account/summary'), [], { refreshMs: 15_000 })
  const topups = useLoad(() => api.get('/portal/topups'), [], { refreshMs: 15_000 })
  const [offset, setOffset] = useState(0)
  const ledger = useLoad(() => api.get(`/portal/ledger?limit=${LIMIT}&offset=${offset}`), [offset], { refreshMs: 15_000 })
  const [amount, setAmount] = useState('100000')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const s = sum.data
  const last = topups.data?.items?.[0]

  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setError('')
    try {
      await api.post('/portal/topups', { amount: somToMicro(amount), note })
      toast(tr("So'rov yuborildi. Administrator tasdiqlashini kuting."))
      setNote(''); topups.reload(); sum.reload()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  if (freeMode) {
    return (
      <>
        <PageHead title={tr("Hisob")} lead={tr("Xizmat hozircha bepul.")} />
        <FreeQuota quota={s?.free_daily} style={{ marginBottom: 18 }} />
        <Empty title={tr("Xizmat hozircha bepul")}><span>{tr("Hamkorlar uchun pul yechilmaydi va hisobni to'ldirish shart emas. Har bir so'rovning tokenlarini")} <Link to="/app/usage">{tr("Sarf va cheklar")}</Link> {tr("bo'limida ko'rasiz.")}</span></Empty>
      </>
    )
  }

  return (
    <>
      <PageHead
        crumbs={<nav className="crumbs" aria-label={tr("Yo'l")}><Link to="/app">{tr("Bosh sahifa")}</Link><i>/</i><span>{tr("Hisob")}</span></nav>}
        title={tr("Hisob")}
        lead={tr("Hisobingizni to'ldiring va API xizmatlaridan foydalanishni davom ettiring.")}
      />
      <ErrorNote error={sum.error} retry={sum.reload} />
      {!s && !sum.error && <Loading rows={2} />}

      <div className="cols">
        <div className="vstack">
          {s && (
            <div className="balance-card">
              <div className="hstack" style={{ gap: 18, flexWrap: 'nowrap' }}>
                <span className="ico-chip ico-chip--lg"><IconWallet /></span>
                <div className="balance-big">
                  <span className="label">{tr("Joriy balansingiz")}</span>
                  <span className="figure">{fmtSom(s.balance, { unit: false })}<small>{tr("so'm")}</small></span>
                  {s.held > 0 && <span className="note">{tr("Shundan")} {fmtSom(s.held)} {tr("hozir bajarilayotgan so'rovlar uchun band.")}</span>}
                </div>
              </div>
              <a className="btn btn--brand btn--large" href="#toldirish"><IconZap /> {tr("Hisobni to'ldirish")}</a>
            </div>
          )}
          <FreeQuota quota={s?.free_daily} />

          <section className="panel" id="toldirish">
            <div className="panel-head">
              <span className="ico-chip"><IconWallet /></span>
              <div className="panel-head__t" style={{ flex: 1 }}>
                <h2>{tr("Hisobni to'ldirish")}</h2>
                <p>{tr("Quyidagi ma'lumotlar asosida to'lov qilib, balansingizni to'ldiring.")}</p>
              </div>
            </div>

            <div className="steps-card">
              <div className="step-row">
                <span className="step-no">1</span>
                <div>
                  <b>{tr("To'lovni amalga oshiring")}</b>
                  <p>{tr(s?.payment_info
                    ? tr("Quyidagi ma'lumotlar bo'yicha istalgan bank ilovasi orqali to'lov qiling va to'lov summasini kiriting.")
                    : tr("To'lov usullari haqida administrator sizga xabar beradi. So'rovni yuborganingizdan keyin to'lovni amalga oshiring."))}</p>
                </div>
              </div>
              {s?.payment_info && <pre className="pay-info">{s.payment_info}</pre>}
            </div>

            <div className="step-row" style={{ marginTop: 20 }}>
              <span className="step-no step-no--ok">2</span>
              <div style={{ minWidth: 0 }}>
                <b>{tr("To'ldirish so'rovini yuborish")}</b>
                <p>{tr("To'lov qilgan summangizni va chek ma'lumotlarini kiriting.")}</p>
              </div>
            </div>

            <form className="form" onSubmit={submit} style={{ marginTop: 14 }}>
              {error && <div className="alert alert--error" role="alert">{tr(error)}</div>}
              <div className="field">
                <span className="field-label">{tr("To'lov summasi")}</span>
                <div className="presets" role="group" aria-label={tr("Tez tanlash")}>
                  {PRESETS.map((p) => (
                    <button type="button" key={p} className={`btn btn--small ${Number(amount) === p ? 'btn--primary' : ''}`} onClick={() => setAmount(String(p))}>{fmtNumber(p)}</button>
                  ))}
                </div>
              </div>
              <label className="field">
                <span className="sr-only">{tr("Summa, so'm")}</span>
                <span className="suffix-field">
                  <input className="input" type="number" min="1" step="1" required value={amount} onChange={(e) => setAmount(e.target.value)} aria-label={tr("Summa, so'm")} />
                  <span className="suffix">{tr("so'm")}</span>
                </span>
                {s && <span className="hint">{tr("Eng kam summa:")} {fmtSom(s.min_topup)}</span>}
              </label>
              <label className="field">{tr("To'lov vaqti, kartaning oxirgi raqamlari yoki chek raqami (ixtiyoriy)")} <input className="input" value={note} onChange={(e) => setNote(e.target.value)} maxLength={300} placeholder={tr("Masalan: 19-okt, 03:24, karta oxirgi 5320 yoki chek raqami...")} />
              </label>
              <button className="btn btn--primary btn--large" disabled={busy || !(Number(amount) > 0)}>
                {tr(busy ? tr("Yuborilmoqda...") : <>{tr("To'ldirish so'rovini yuborish")} <IconArrowRight /></>)}
              </button>
            </form>
          </section>
        </div>

        <div className="vstack">
          <section className="panel">
            <div className="panel-head">
              <span className="ico-chip"><IconChart /></span>
              <div className="panel-head__t" style={{ flex: 1 }}>
                <h2>{tr("Doimiy online")}</h2>
                <p>{tr("Balansingiz yetarli bo'lsa, API so'rovlaringiz uzluksiz ishlaydi.")}</p>
              </div>
            </div>
          </section>

          <section className="panel">
            <div className="panel-head">
              <span className="ico-chip"><IconClock /></span>
              <div className="panel-head__t" style={{ flex: 1 }}><h2>{tr("So'nggi so'rov")}</h2></div>
            </div>
            {!last && <p className="muted small">{tr("Hali to'ldirish so'rovi yubormagansiz.")}</p>}
            {last && (
              <div className="mini-card">
                <span className="mini-card__label">{fmtDateTime(last.created_at)}</span>
                <div className="hstack" style={{ justifyContent: 'space-between' }}>
                  <b className="mini-card__fig">{fmtSom(last.amount)}</b>
                  <span className={`tag ${STATUS[last.status][1]}`}>{tr(STATUS[last.status][0])}</span>
                </div>
                {last.admin_note && <span className="dim small">{tr(last.admin_note)}</span>}
              </div>
            )}
          </section>

          <section className="panel">
            <div className="panel-head">
              <span className="ico-chip"><IconShield /></span>
              <div className="panel-head__t" style={{ flex: 1 }}><h2>{tr("Muhim ma'lumotlar")}</h2></div>
            </div>
            <div className="steps-card steps-card--mint">
              <div className="step-row">
                <span className="step-no step-no--ok">1</span>
                <div><b>{tr("To'lovni faqat yuqoridagi ma'lumotlar bo'yicha qiling.")}</b><p>{tr("Boshqa rekvizitlarga qilingan to'lovlar hisobga olinmaydi.")}</p></div>
              </div>
              <div className="step-row">
                <span className="step-no step-no--ok">2</span>
                <div><b>{tr("To'lovdan so'ng, ushbu sahifa orqali so'rov yuboring.")}</b><p>{tr("To'lov summasi va chek ma'lumotlarini kiriting.")}</p></div>
              </div>
              <div className="step-row">
                <span className="step-no step-no--ok">3</span>
                <div><b>{tr("So'rov ko'rib chiqilgandan keyin balans to'ldiriladi.")}</b><p>{tr("Tasdiqlangandan so'ng balansingiz avtomatik ravishda yangilanadi.")}</p></div>
              </div>
            </div>
          </section>

          <section className="help-card">
            <span className="ico-chip ico-chip--sky"><IconHeadset /></span>
            <b>{tr("Savollaringiz bormi?")}</b>
            <p>{tr("To'lov bilan bog'liq savollar uchun administrator bilan bog'laning.")}</p>
            <Link to="/app/docs" className="btn btn--small"><IconSend /> {tr("Hujjatlarni ochish")}</Link>
          </section>
        </div>
      </div>

      {/* div, not section: `.block:first-of-type` zeroes the top margin, and as a section it would be the first section on the page */}
      <div className="panel block">
        <div className="panel-head">
          <div className="panel-head__t"><h2>{tr("Hisob tarixi")}</h2><p>{tr("To'ldirish va so'rovlar bo'yicha barcha operatsiyalar.")}</p></div>
        </div>
        <ErrorNote error={ledger.error} retry={ledger.reload} />
        {ledger.data && ledger.data.items.length === 0 && <Empty title={tr("Hali operatsiya yo'q")}>{tr("To'ldirish va so'rovlar shu yerda ko'rinadi.")}</Empty>}
        {ledger.data && ledger.data.items.length > 0 && (
          <>
            <div className="table-wrap"><table className="table">
              <thead><tr><th>{tr("Sana")}</th><th>{tr("Tur")}</th><th>{tr("Izoh")}</th><th className="num">{tr("Summa")}</th><th className="num">{tr("Qoldiq")}</th></tr></thead>
              <tbody>
                {ledger.data.items.map((l) => (
                  <tr key={l.id}>
                    <td className="muted nowrap">{fmtDateTime(l.created_at)}</td>
                    <td><span className="tag">{tr(KIND[l.kind] || l.kind)}</span></td>
                    <td className="muted">{tr(l.note)}</td>
                    <td className="num"><b>{fmtSom(l.amount, { sign: true })}</b></td>
                    <td className="num muted">{fmtSom(l.balance_after)}</td>
                  </tr>
                ))}
              </tbody>
            </table></div>
            <Pager total={ledger.data.total} limit={LIMIT} offset={offset} onChange={setOffset} />
          </>
        )}
      </div>
    </>
  )
}
