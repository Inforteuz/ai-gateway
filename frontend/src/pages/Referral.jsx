import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { Link } from 'react-router-dom'
import { api } from '../api.js'
import { useSite } from '../auth.js'
import { CopyButton, CopyIcon, Empty, ErrorNote, Loading, PageHead, StatCard, useLoad } from '../components/ui.jsx'
import { IconCoins, IconLink, IconSend, IconSparkles, IconUsers, IconWallet } from '../components/icons.jsx'
import { fmtDateTime, fmtInt, fmtSom } from '../format.js'

export default function Referral() {
  useLocaleRefresh()
  const site = useSite()
  const { data: r, error, reload } = useLoad(() => api.get('/portal/referral'), [], { refreshMs: 30_000 })
  const share = r ? `https://t.me/share/url?url=${encodeURIComponent(r.link)}&text=${encodeURIComponent(tr("{v0} — shu havola orqali ro'yxatdan o'ting", { v0: (site.brand_name) }))}` : ''

  return (
    <>
      <PageHead
        crumbs={<nav className="crumbs" aria-label={tr("Yo'l")}><Link to="/app">{tr("Bosh sahifa")}</Link><i>/</i><span>{tr("Do'stlarni taklif qilish")}</span></nav>}
        title={tr("Do'stlarni taklif qiling")}
        lead={r
          ? tr("Havolangiz orqali ro'yxatdan o'tgan do'stingiz hisobini har safar to'ldirganda, to'lov summasining {v0}% i sizning balansingizga tushadi.", { v0: (r.percent) })
          : tr("Havolangiz orqali ro'yxatdan o'tgan do'stlaringizning har bir to'lovidan mukofot oling.")}
      />
      <ErrorNote error={error} retry={reload} />
      {!r && !error && <Loading rows={3} />}

      {r && (
        <>
          {!r.enabled && (
            <div className="alert" role="status" style={{ marginBottom: 18 }}> {tr("Taklif dasturi hozircha to'xtatilgan: yangi takliflar va mukofotlar hisoblanmaydi. Oldin ishlagan mukofotlaringiz balansingizda qoladi.")} </div>
          )}

          <div className="cols">
            <section className="panel">
              <div className="panel-head">
                <span className="ico-chip"><IconLink /></span>
                <div className="panel-head__t" style={{ flex: 1 }}>
                  <h2>{tr("Sizning taklif havolangiz")}</h2>
                  <p>{tr("Havolani do'stlaringizga yuboring. Ular shu havola orqali (email, Google yoki Telegram bilan) ro'yxatdan o'tishi kerak.")}</p>
                </div>
              </div>
              <div className="copy-row">
                <div className="who"><span>{tr("Havola")}</span><b className="mono" style={{ whiteSpace: 'normal', overflow: 'visible', overflowWrap: 'anywhere' }}>{tr(r.link)}</b></div>
                <CopyIcon text={r.link} title={tr("Havolani nusxalash")} />
              </div>
              <div className="hstack" style={{ marginTop: 12, gap: 10 }}>
                <CopyButton text={r.link} label={tr("Havolani nusxalash")} small={false} />
                <a className="btn" href={share} target="_blank" rel="noopener noreferrer"><IconSend /> {tr("Telegramda ulashish")}</a>
                <span className="dim small">{tr("Kod:")} <b className="mono">{tr(r.code)}</b></span>
              </div>
            </section>

            <section className="panel">
              <div className="panel-head">
                <span className="ico-chip"><IconSparkles /></span>
                <div className="panel-head__t" style={{ flex: 1 }}><h2>{tr("Qanday ishlaydi")}</h2></div>
              </div>
              <div className="steps-card steps-card--mint">
                <div className="step-row">
                  <span className="step-no step-no--ok">1</span>
                  <div><b>{tr("Havolani yuboring.")}</b><p>{tr("Do'stingiz havolani ochib, yangi hisob ochadi.")}</p></div>
                </div>
                <div className="step-row">
                  <span className="step-no step-no--ok">2</span>
                  <div><b>{tr("Do'stingiz hisobini to'ldiradi.")}</b><p>{tr("Har bir tasdiqlangan to'lov hisobga olinadi — birinchisigina emas.")}</p></div>
                </div>
                <div className="step-row">
                  <span className="step-no step-no--ok">3</span>
                  <div><b>{tr(r.percent)}{tr("% balansingizga tushadi.")}</b><p>{tr("Sovg'alar, tuzatishlar va mukofotlarning o'zidan foiz hisoblanmaydi. Allaqachon ochilgan hisobni keyin bog'lab bo'lmaydi.")}</p></div>
                </div>
              </div>
            </section>
          </div>

          <div className="metric-grid" style={{ marginTop: 18 }}>
            <StatCard icon={<IconUsers />} label={tr("Taklif qilinganlar")} value={fmtInt(r.invited)} unit="kishi" />
            <StatCard icon={<IconWallet />} label={tr("To'lov qilganlar")} value={fmtInt(r.depositors)} unit="kishi" tone="sky" />
            <StatCard icon={<IconCoins />} label={tr("Jami mukofot")} value={fmtSom(r.earned, { unit: false })} unit="so'm" tone="amber" />
          </div>

          <div className="panel block">
            <div className="panel-head">
              <div className="panel-head__t"><h2>{tr("Oxirgi mukofotlar")}</h2><p>{tr("Har bir mukofot balansingizga avtomatik qo'shiladi va")} <Link to="/app/billing">{tr("Hisob tarixi")}</Link>{tr("da ko'rinadi.")}</p></div>
            </div>
            {r.rewards.length === 0 && <Empty title={tr("Hali mukofot yo'q")}>{tr("Do'stingiz hisobini to'ldirganda mukofot shu yerda paydo bo'ladi.")}</Empty>}
            {r.rewards.length > 0 && (
              <div className="table-wrap"><table className="table">
                <thead><tr><th>{tr("Do'stingiz to'lovi")}</th><th className="num">{tr("Mukofot")}</th></tr></thead>
                <tbody>
                  {r.rewards.map((x, i) => (
                    <tr key={i}>
                      <td><span className="nowrap">{fmtSom(x.deposit_amount)}</span> <span className="muted">· {tr(x.referee)}</span><div className="dim small">{fmtDateTime(x.created_at)}</div></td>
                      <td className="num"><b className="nowrap">{fmtSom(x.amount, { sign: true })}</b><div className="dim small">{tr(x.percent)}%</div></td>
                    </tr>
                  ))}
                </tbody>
              </table></div>
            )}
          </div>
        </>
      )}
    </>
  )
}
