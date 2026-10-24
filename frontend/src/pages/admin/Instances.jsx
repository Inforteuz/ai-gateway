import { translateText as tr, useLang as useLocaleRefresh } from '../../i18n/index.jsx'
import { useState } from 'react'
import { api } from '../../api.js'
import { Empty, ErrorNote, Loading, Modal, useLoad, useToast } from '../../components/ui.jsx'
import { fmtAgo, fmtDateTime } from '../../format.js'

// Cluster browser instances: status and control without opening VNC.
// No action restarts Chrome — login cookies are preserved.

const STATE = {
  ok: { label: 'Ishlayapti', tone: 'is-ok', dot: 'dot--on' },
  resting: { label: 'Dam olyapti', tone: 'is-cool', dot: 'dot--off' },
  login: { label: 'Login kerak', tone: 'is-cool', dot: 'dot--off' },
  captcha: { label: 'Bot tekshiruvi — VNC orqali yeching', tone: 'is-cool', dot: 'dot--off' },
  crashed: { label: 'Sahifa qulagan', tone: 'is-cool', dot: 'dot--off' },
  no_tab: { label: 'Provayder tabi topilmadi', tone: 'is-cool', dot: 'dot--off' },
  down: { label: 'Brauzer javob bermayapti', tone: 'is-cool', dot: 'dot--off' },
}

const ACTIONS = {
  probe: 'Sessiyani tekshirish',
  reload: 'Sahifani qayta yuklash',
  cleanup: 'Chatlarni tozalash',
  cleanup_dry: 'Tozalash (quruq sinov)',
  vnc_open: 'VNC ochildi',
  vnc_session: 'VNC sessiyasi',
  recover_memory: 'Tabni tiklash (xotira)',
  memory_recovered: 'Avto: tab tiklandi (xotira)',
  memory_recover_unverified: 'Avto: tab tiklandi, sessiya tasdiqlanmadi',
  memory_session_lost: 'Avto: tiklashdan keyin sessiya yo\'qoldi',
  memory_reopen_failed: 'Avto: sayt tabi qayta ochilmadi',
  memory_recover_failed: 'Avto: tiklash xatosi',
  memory_skipped: 'Avto: tiklash o\'tkazib yuborildi',
}

const BLOCK_REASON = {
  login_required: 'login kerak',
  captcha: 'bot tekshiruvi',
  reopen_failed: 'sayt tabi qayta ochilmagan',
}

// Renderer memory: RAM + swap of the site's process (the cluster reads it from /proc)
function MemoryBar({ mem, threshold }) {
  useLocaleRefresh()
  if (!mem || mem.renderer_rss_mb == null) return null
  const total = mem.renderer_rss_mb + mem.renderer_swap_mb
  const pct = threshold ? Math.min(100, Math.round((total / threshold) * 100)) : 0
  const tone = mem.over_threshold || pct >= 100 ? 'is-bad' : pct >= 75 ? 'is-warn' : ''
  return (
    <div className={`inst-mem ${tone}`}>
      <div className="inst-mem__top">
        <span>{tr("Xotira")}</span>
        <b>{tr(total)} MB{tr(threshold ? <small> / {tr(threshold)}</small> : null)}</b>
      </div>
      {tr(threshold ? <div className="inst-mem__bar"><i style={{ width: `${pct}%` }} /></div> : null)}
      <div className="inst-mem__sub">
        RAM {tr(mem.renderer_rss_mb)} {tr("· swap")} {tr(mem.renderer_swap_mb)}
        {mem.recoveries_24h > 0 && <> {tr("· 24 soatda")} {tr(mem.recoveries_24h)} {tr("tiklash")}</>}
      </div>
    </div>
  )
}

// Per-provider cleanup policy (cluster `CLEANUP_POLICY`). The rules live in the cluster —
// this is only the explanation.
const POLICY = {
  registry: {
    card: (c) => tr("Shlyuz chatlari · reyestrda {v0} ta", { v0: (c.registry_size ?? '?') }),
    rule: <>{tr("Faqat")} <b>{tr("shlyuz o'zi yaratgan")}</b> {tr("(reyestrdagi), 7 kundan yangi va pin qilinmagan chatlar o'chiriladi. Reyestrda yo'q chatlarga — jumladan shaxsiy chatlaringizga — tegilmaydi.")}</>,
  },
  pin: {
    card: () => tr("Pin qilinmaganlar"),
    rule: <><b>{tr("Pin qilinmagan")}</b> {tr("chatlar o'chiriladi (eng yangilari qoladi). Saqlab qolmoqchi bo'lgan chatni avval provayderda pin qiling — pin qilinganlarga tegilmaydi.")}</>,
  },
  all: {
    card: () => tr("Barcha chatlar"),
    rule: <>{tr("Bu akkauntdagi")} <b>{tr("barcha")}</b> {tr("chatlar o'chiriladi — egasi bu akkauntdagi chatlarning hammasi keraksiz ekanini tasdiqlagan.")}</>,
  },
}

function TitleList({ title, items, extra }) {
  useLocaleRefresh()
  if (!items?.length) return null
  return (
    <details style={{ marginTop: 8 }}>
      <summary className="small">{tr(title)} ({tr(items.length)}{tr(extra ? '+' : '')})</summary>
      <ol className="small" style={{ maxHeight: 220, overflow: 'auto', margin: '6px 0 0', paddingLeft: 22 }}>
        {items.map((t, i) => <li key={i}>{t || <span className="dim">{tr("(sarlavhasiz)")}</span>}</li>)}
      </ol>
    </details>
  )
}

function InstanceCard({ it, vncEnabled, threshold, onDone }) {
  useLocaleRefresh()
  const toast = useToast()
  const [pending, setPending] = useState('')
  const [plan, setPlan] = useState(null)
  const [confirmRecover, setConfirmRecover] = useState(false)
  const mem = it.memory
  const restartsChrome = mem?.method === 'restart'
  const st = STATE[it.state] || { label: it.state, tone: '', dot: 'dot--off' }
  const tab = it.tabs?.[0]
  const cleanup = it.tabs?.find((t) => t.cleanup?.supported)?.cleanup
  const busy = !!pending || it.busy

  const run = async (action, body) => {
    setPending(action)
    try {
      const r = await api.post(`/portal/admin/instances/${it.instance}/${action}`, body)
      toast(r.summary || 'Bajarildi', r.ok === false ? 'error' : 'ok')
      return r
    } catch (e) {
      toast(e.message, 'error')
      return null
    } finally {
      setPending('')
      onDone()
    }
  }

  const dryRun = async () => {
    const r = await run('cleanup', { dry_run: true })
    if (r) setPlan(r.tabs?.[0] || {})
  }

  const openVnc = () => window.open(`/app/admin/vnc/${it.instance}`, `sf-vnc-${it.instance}`, 'noopener')

  return (
    <div className={`inst-card ${st.tone}`}>
      <div className="inst-card__head">
        <b>#{tr(it.instance)} {tr(it.models?.join(', ') || '—')}</b>
        <small>:{tr(it.port)}</small>
      </div>
      <div className="inst-card__state">
        <span className={`dot ${st.dot}`} />
        {tr(st.label)}
        {tab?.cooldown_left > 0 && <span className="muted"> · {tr(tab.cooldown_left)}{tr("s")}</span>}
        {it.busy && <span className="muted"> {tr("· amal bajarilyapti…")}</span>}
      </div>

      <MemoryBar mem={mem} threshold={threshold} />

      <dl className="inst-card__facts">
        {tab?.last_probe && <>
          <dt>{tr("Sessiya")}</dt>
          <dd>{tr(tab.last_probe.ok ? 'joyida' : tab.last_probe.reason)} · {fmtAgo(tab.last_probe.ts)}</dd>
        </>}
        {cleanup && <>
          <dt>{tr("Tozalash")}</dt>
          <dd title={POLICY[cleanup.policy] ? undefined : cleanup.policy}>{tr(POLICY[cleanup.policy]?.card(cleanup) || cleanup.policy)}</dd>
        </>}
        {mem?.last_result && <>
          <dt>{tr("Tiklash")}</dt>
          <dd className="inst-clip" title={tr(mem.last_result.summary)}>{tr(mem.last_result.ok ? 'muvaffaqiyatli' : 'muammo')} · {fmtAgo(mem.last_result.ts)}</dd>
        </>}
        {it.last_action && <>
          <dt>{tr("Oxirgi amal")}</dt>
          <dd className="inst-clip" title={it.last_action.summary || ''}>{tr(ACTIONS[it.last_action.action] || it.last_action.action)} · {fmtAgo(it.last_action.ts)}</dd>
        </>}
      </dl>

      {(it.last_error || mem?.blocked) && (
        <div className="inst-card__alert">
          {mem?.blocked && (
            <div title={tr("VNC orqali tekshiring; sessiya joyida bo'lsa, \"RAM bo'shatish\" blokni olib tashlaydi.")}> {tr("Avto-tiklash to'xtatilgan:")} {tr(BLOCK_REASON[mem.blocked.reason] || mem.blocked.reason)} ({fmtAgo(mem.blocked.ts)})
            </div>
          )}
          {it.last_error && <div className="inst-clip" title={tr(it.last_error)}>{tr(it.last_error)}</div>}
        </div>
      )}

      <div className="inst-card__actions">
        <button type="button" className="btn btn--small" disabled={busy} onClick={() => run('probe')}>
          {tr(pending === 'probe' ? tr("Tekshirilmoqda…") : 'Tekshirish')}
        </button>
        <button type="button" className="btn btn--small" disabled={busy} onClick={() => run('reload')}>
          {tr(pending === 'reload' ? tr("Yuklanmoqda…") : tr("Qayta yuklash"))}
        </button>
        {cleanup && (
          <button type="button" className="btn btn--small" disabled={busy} onClick={dryRun}>
            {tr(pending === 'cleanup' ? tr("Hisoblanmoqda…") : tr("Chat tozalash"))}
          </button>
        )}
        {tab && (
          <button type="button" className="btn btn--small" disabled={busy} onClick={() => setConfirmRecover(true)}
                  title={restartsChrome ? tr("Chrome'ni qayta ishga tushirish — login saqlanadi")
                                        : tr("Tabni yangi renderer'da qayta ochish — Chrome qayta ishga tushmaydi")}>
            {tr(pending === 'recover-memory' ? tr("Boshlanmoqda…") : tr("RAM bo'shatish"))}
          </button>
        )}
        <button type="button" className="btn btn--small btn--primary inst-card__vnc" disabled={!vncEnabled} onClick={openVnc}> {tr("VNC'ni ochish")} </button>
      </div>
      {confirmRecover && (
        <Modal title={`#${it.instance}: tabni tiklash`} onClose={() => setConfirmRecover(false)}
          actions={<>
            <button type="button" className="btn" onClick={() => setConfirmRecover(false)}>{tr("Bekor qilish")}</button>
            <button type="button" className="btn btn--primary"
                    onClick={() => { setConfirmRecover(false); run('recover-memory') }}> {tr("Tiklash")} </button>
          </>}>
          {restartsChrome
            ? <p>{tr("Bu instansiyada tabni qayta ochish xotirani bo'shatmaydi, shuning uchun")} <b>{tr("Chrome qayta ishga tushiriladi")}</b> {tr("(faqat shu instansiya). Login diskda saqlanadi va restartdan omon qoladi; oldin cookie zaxirasi olinadi.")}</p>
            : <p>{tr("Provayder tabi yopilib, sayt yangi tabda (yangi renderer jarayonida) qayta ochiladi — renderer to'plagan xotira bo'shaydi. Chrome qayta ishga tushirilmaydi, login cookie'lariga tegilmaydi.")}</p>}
          <ul className="small" style={{ marginTop: 8 }}>
            <li>{tr("Tiklashdan oldin va keyin sessiya tekshiriladi; sessiya yo'qolsa — Telegram'ga xabar ketadi va avto-tiklash shu instansiyada to'xtatiladi.")}</li>
            <li>{tr("Jonli so'rov tugashi kutiladi (30 soniyagacha); tiklash odatda 15–40 soniya oladi va bu vaqtda instansiya so'rov qabul qilmaydi.")}</li>
            {mem?.renderer_rss_mb != null && <li>{tr("Hozir:")} {tr(mem.renderer_rss_mb)} {tr("MB RAM +")} {tr(mem.renderer_swap_mb)} {tr("MB swap.")}</li>}
          </ul>
        </Modal>
      )}
      {plan && (
        <Modal title={tr("#{v0}: eski chatlarni tozalash", { v0: (it.instance) })} onClose={() => setPlan(null)}
          actions={<>
            <button type="button" className="btn" onClick={() => setPlan(null)}>{tr("Bekor qilish")}</button>
            <button type="button" className="btn btn--danger" disabled={!plan.would_delete}
                    onClick={() => { setPlan(null); run('cleanup', { dry_run: false }) }}>
              {tr(plan.would_delete ? tr("{v0} ta chatni o'chirish", { v0: (plan.would_delete) }) : tr("O'chiriladigan chat yo'q"))}
            </button>
          </>}>
          <p>{tr(POLICY[plan.policy]?.rule)}</p>
          <ul className="small" style={{ marginTop: 8 }}>
            <li>{tr("O'chiriladi:")} <b>{tr(plan.would_delete ?? 0)}</b>{tr(plan.later > 0 ? tr("(yana {v0} tasi keyingi yurishlarda)", { v0: (plan.later) }) : '')}</li>
            <li>{tr("Himoyalangan / qoladi:")} {tr(plan.protected ?? 0)}</li>
            {plan.skipped > 0 && <li>{tr("O'tkazib yuborildi (holatini o'qib bo'lmadi):")} {tr(plan.skipped)}</li>}
            <li>{tr("Yon panelda:")} {tr(plan.rows ?? 0)}{tr(plan.registry_size != null ? `, reyestrda: ${plan.registry_size}` : '')}{tr(plan.complete === false ? tr("(ro'yxat to'liq yuklanmadi)") : '')}</li>
          </ul>
          <TitleList title={tr("O'chiriladigan chatlar")} items={plan.delete_titles} extra={plan.would_delete > (plan.delete_titles?.length || 0)} />
          <TitleList title={tr("Qoladigan chatlar")} items={plan.protect_titles} extra={plan.protected > (plan.protect_titles?.length || 0)} />
          <p className="muted small" style={{ marginTop: 8 }}>{tr("O'chirish fonda bajariladi (~5–7 soniya/chat); bu vaqtda shu instansiya so'rov qabul qilmaydi. Natija pastdagi jurnalda ko'rinadi.")}</p>
        </Modal>
      )}
    </div>
  )
}

export default function Instances() {
  useLocaleRefresh()
  const { data, error, reload } = useLoad(() => api.get('/portal/admin/instances'), [], { refreshMs: 10_000 })
  const items = data?.instances || []
  const sys = data?.system
  const guard = data?.memory_guard

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{tr("Brauzer instansiyalari")}</h1>
          <p>{tr("Har instansiya holati va VNC'ga kirmasdan boshqarish. Amallar Chrome'ni qayta ishga tushirmaydi — loginlar saqlanadi. Sahifa 10 soniyada yangilanadi.")}</p>
        </div>
        <button type="button" className="btn btn--small" onClick={reload}>{tr("Yangilash")}</button>
      </div>
      <ErrorNote error={error} retry={reload} />
      {!data && !error && <Loading rows={3} />}
      {data && !data.vnc_enabled && (
        <div className="alert alert--warn" style={{ marginBottom: 16 }}>{tr("VNC sozlanmagan: serverda")} <code>SF_VNC_PASSWORD</code> {tr("berilmagan.")}</div>
      )}
      {sys?.swap_total_mb > 0 && (
        <div className={`alert ${sys.swap_percent >= 80 ? 'alert--warn' : ''}`} style={{ marginBottom: 16 }}>
          {/* "swap" is kept next to the number: wrapped onto its own line it read as
              though 5% of the SERVER were busy, which is a different thing entirely. */} {tr("Server:")} <b style={{ whiteSpace: 'nowrap' }}>{tr("swap")} {tr(sys.swap_percent)}{tr("% band")}</b>{' '}
          ({tr(sys.swap_used_mb)} / {tr(sys.swap_total_mb)} {tr("MB),")} <span style={{ whiteSpace: 'nowrap' }}>{tr("bo'sh RAM")} {tr(sys.mem_available_mb)} / {tr(sys.mem_total_mb)} MB</span>.
          {guard && (guard.enabled
            ? <span className="dim"> {tr("Avto-tiklash: renderer")} {tr(guard.threshold_mb)} {tr("MB dan oshsa, eng kami")} {Math.round(guard.min_gap / 60)} {tr("daqiqa oraliq bilan, kuniga")} {tr(guard.max_per_day)} {tr("martagacha.")}</span>
            : <span className="dim"> {tr("Avto-tiklash o'chiq.")}</span>)}
        </div>
      )}
      {data && items.length === 0 && <Empty title={tr("Instansiya yo'q")}>{tr("Klasterda CDP porti sozlanmagan.")}</Empty>}
      {items.length > 0 && (
        <div className="inst-grid">
          {items.map((it) => <InstanceCard key={it.port} it={it} vncEnabled={!!data.vnc_enabled} threshold={guard?.threshold_mb} onDone={reload} />)}
        </div>
      )}
      {data?.audit && (
        <div className="block panel">
          <div className="panel-head"><h2>{tr("Amallar jurnali")}</h2></div>
          {data.audit.length === 0 ? <Empty title={tr("Hali amal bajarilmagan")} /> : (
            <div className="table-wrap"><table className="table">
              <thead><tr><th>{tr("Vaqt")}</th><th>{tr("Kim")}</th><th>{tr("Instansiya")}</th><th>{tr("Amal")}</th><th>{tr("Natija")}</th></tr></thead>
              <tbody>
                {data.audit.map((a) => (
                  <tr key={a.id}>
                    <td className="muted nowrap">{fmtDateTime(a.created_at)}</td>
                    <td>{tr(a.admin_email)}</td>
                    <td>#{a.target.replace('instance:', '')}</td>
                    <td>{tr(ACTIONS[a.action] || a.action)}</td>
                    <td><span className={`tag ${a.ok ? 'tag--ok' : 'tag--bad'}`}>{tr(a.ok ? 'OK' : tr("Xato"))}</span> <span className="dim small">{tr(a.detail)}</span></td>
                  </tr>
                ))}
              </tbody>
            </table></div>
          )}
        </div>
      )}
    </>
  )
}
