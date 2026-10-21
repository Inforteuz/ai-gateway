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
  if (!mem || mem.renderer_rss_mb == null) return null
  const total = mem.renderer_rss_mb + mem.renderer_swap_mb
  const pct = threshold ? Math.min(100, Math.round((total / threshold) * 100)) : 0
  const tone = mem.over_threshold || pct >= 100 ? 'is-bad' : pct >= 75 ? 'is-warn' : ''
  return (
    <div className={`inst-mem ${tone}`}>
      <div className="inst-mem__top">
        <span>Xotira</span>
        <b>{total} MB{threshold ? <small> / {threshold}</small> : null}</b>
      </div>
      {threshold ? <div className="inst-mem__bar"><i style={{ width: `${pct}%` }} /></div> : null}
      <div className="inst-mem__sub">
        RAM {mem.renderer_rss_mb} · swap {mem.renderer_swap_mb}
        {mem.recoveries_24h > 0 && <> · 24 soatda {mem.recoveries_24h} tiklash</>}
      </div>
    </div>
  )
}

// Per-provider cleanup policy (cluster `CLEANUP_POLICY`). The rules live in the cluster —
// this is only the explanation.
const POLICY = {
  registry: {
    card: (c) => `Shlyuz chatlari · reyestrda ${c.registry_size ?? '?'} ta`,
    rule: <>Faqat <b>shlyuz o'zi yaratgan</b> (reyestrdagi), 7 kundan yangi va pin qilinmagan chatlar o'chiriladi. Reyestrda yo'q chatlarga — jumladan shaxsiy chatlaringizga — tegilmaydi.</>,
  },
  pin: {
    card: () => 'Pin qilinmaganlar',
    rule: <><b>Pin qilinmagan</b> chatlar o'chiriladi (eng yangilari qoladi). Saqlab qolmoqchi bo'lgan chatni avval provayderda pin qiling — pin qilinganlarga tegilmaydi.</>,
  },
  all: {
    card: () => 'Barcha chatlar',
    rule: <>Bu akkauntdagi <b>barcha</b> chatlar o'chiriladi — egasi bu akkauntdagi chatlarning hammasi keraksiz ekanini tasdiqlagan.</>,
  },
}

function TitleList({ title, items, extra }) {
  if (!items?.length) return null
  return (
    <details style={{ marginTop: 8 }}>
      <summary className="small">{title} ({items.length}{extra ? '+' : ''})</summary>
      <ol className="small" style={{ maxHeight: 220, overflow: 'auto', margin: '6px 0 0', paddingLeft: 22 }}>
        {items.map((t, i) => <li key={i}>{t || <span className="dim">(sarlavhasiz)</span>}</li>)}
      </ol>
    </details>
  )
}

function InstanceCard({ it, vncEnabled, threshold, onDone }) {
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
        <b>#{it.instance} {it.models?.join(', ') || '—'}</b>
        <small>:{it.port}</small>
      </div>
      <div className="inst-card__state">
        <span className={`dot ${st.dot}`} />
        {st.label}
        {tab?.cooldown_left > 0 && <span className="muted"> · {tab.cooldown_left}s</span>}
        {it.busy && <span className="muted"> · amal bajarilyapti…</span>}
      </div>

      <MemoryBar mem={mem} threshold={threshold} />

      <dl className="inst-card__facts">
        {tab?.last_probe && <>
          <dt>Sessiya</dt>
          <dd>{tab.last_probe.ok ? 'joyida' : tab.last_probe.reason} · {fmtAgo(tab.last_probe.ts)}</dd>
        </>}
        {cleanup && <>
          <dt>Tozalash</dt>
          <dd title={POLICY[cleanup.policy] ? undefined : cleanup.policy}>{POLICY[cleanup.policy]?.card(cleanup) || cleanup.policy}</dd>
        </>}
        {mem?.last_result && <>
          <dt>Tiklash</dt>
          <dd className="inst-clip" title={mem.last_result.summary}>{mem.last_result.ok ? 'muvaffaqiyatli' : 'muammo'} · {fmtAgo(mem.last_result.ts)}</dd>
        </>}
        {it.last_action && <>
          <dt>Oxirgi amal</dt>
          <dd className="inst-clip" title={it.last_action.summary || ''}>{ACTIONS[it.last_action.action] || it.last_action.action} · {fmtAgo(it.last_action.ts)}</dd>
        </>}
      </dl>

      {(it.last_error || mem?.blocked) && (
        <div className="inst-card__alert">
          {mem?.blocked && (
            <div title={`VNC orqali tekshiring; sessiya joyida bo'lsa, "RAM bo'shatish" blokni olib tashlaydi.`}>
              Avto-tiklash to'xtatilgan: {BLOCK_REASON[mem.blocked.reason] || mem.blocked.reason} ({fmtAgo(mem.blocked.ts)})
            </div>
          )}
          {it.last_error && <div className="inst-clip" title={it.last_error}>{it.last_error}</div>}
        </div>
      )}

      <div className="inst-card__actions">
        <button type="button" className="btn btn--small" disabled={busy} onClick={() => run('probe')}>
          {pending === 'probe' ? 'Tekshirilmoqda…' : 'Tekshirish'}
        </button>
        <button type="button" className="btn btn--small" disabled={busy} onClick={() => run('reload')}>
          {pending === 'reload' ? 'Yuklanmoqda…' : 'Qayta yuklash'}
        </button>
        {cleanup && (
          <button type="button" className="btn btn--small" disabled={busy} onClick={dryRun}>
            {pending === 'cleanup' ? 'Hisoblanmoqda…' : 'Chat tozalash'}
          </button>
        )}
        {tab && (
          <button type="button" className="btn btn--small" disabled={busy} onClick={() => setConfirmRecover(true)}
                  title={restartsChrome ? "Chrome'ni qayta ishga tushirish — login saqlanadi"
                                        : "Tabni yangi renderer'da qayta ochish — Chrome qayta ishga tushmaydi"}>
            {pending === 'recover-memory' ? 'Boshlanmoqda…' : 'RAM bo\'shatish'}
          </button>
        )}
        <button type="button" className="btn btn--small btn--primary inst-card__vnc" disabled={!vncEnabled} onClick={openVnc}>
          VNC'ni ochish
        </button>
      </div>
      {confirmRecover && (
        <Modal title={`#${it.instance}: tabni tiklash`} onClose={() => setConfirmRecover(false)}
          actions={<>
            <button type="button" className="btn" onClick={() => setConfirmRecover(false)}>Bekor qilish</button>
            <button type="button" className="btn btn--primary"
                    onClick={() => { setConfirmRecover(false); run('recover-memory') }}>
              Tiklash
            </button>
          </>}>
          {restartsChrome
            ? <p>Bu instansiyada tabni qayta ochish xotirani bo'shatmaydi, shuning uchun <b>Chrome qayta ishga tushiriladi</b> (faqat shu instansiya). Login diskda saqlanadi va restartdan omon qoladi; oldin cookie zaxirasi olinadi.</p>
            : <p>Provayder tabi yopilib, sayt yangi tabda (yangi renderer jarayonida) qayta ochiladi — renderer to'plagan xotira bo'shaydi. Chrome qayta ishga tushirilmaydi, login cookie'lariga tegilmaydi.</p>}
          <ul className="small" style={{ marginTop: 8 }}>
            <li>Tiklashdan oldin va keyin sessiya tekshiriladi; sessiya yo'qolsa — Telegram'ga xabar ketadi va avto-tiklash shu instansiyada to'xtatiladi.</li>
            <li>Jonli so'rov tugashi kutiladi (30 soniyagacha); tiklash odatda 15–40 soniya oladi va bu vaqtda instansiya so'rov qabul qilmaydi.</li>
            {mem?.renderer_rss_mb != null && <li>Hozir: {mem.renderer_rss_mb} MB RAM + {mem.renderer_swap_mb} MB swap.</li>}
          </ul>
        </Modal>
      )}
      {plan && (
        <Modal title={`#${it.instance}: eski chatlarni tozalash`} onClose={() => setPlan(null)}
          actions={<>
            <button type="button" className="btn" onClick={() => setPlan(null)}>Bekor qilish</button>
            <button type="button" className="btn btn--danger" disabled={!plan.would_delete}
                    onClick={() => { setPlan(null); run('cleanup', { dry_run: false }) }}>
              {plan.would_delete ? `${plan.would_delete} ta chatni o'chirish` : "O'chiriladigan chat yo'q"}
            </button>
          </>}>
          <p>{POLICY[plan.policy]?.rule}</p>
          <ul className="small" style={{ marginTop: 8 }}>
            <li>O'chiriladi: <b>{plan.would_delete ?? 0}</b>{plan.later > 0 ? ` (yana ${plan.later} tasi keyingi yurishlarda)` : ''}</li>
            <li>Himoyalangan / qoladi: {plan.protected ?? 0}</li>
            {plan.skipped > 0 && <li>O'tkazib yuborildi (holatini o'qib bo'lmadi): {plan.skipped}</li>}
            <li>Yon panelda: {plan.rows ?? 0}{plan.registry_size != null ? `, reyestrda: ${plan.registry_size}` : ''}{plan.complete === false ? ' (ro\'yxat to\'liq yuklanmadi)' : ''}</li>
          </ul>
          <TitleList title="O'chiriladigan chatlar" items={plan.delete_titles} extra={plan.would_delete > (plan.delete_titles?.length || 0)} />
          <TitleList title="Qoladigan chatlar" items={plan.protect_titles} extra={plan.protected > (plan.protect_titles?.length || 0)} />
          <p className="muted small" style={{ marginTop: 8 }}>O'chirish fonda bajariladi (~5–7 soniya/chat); bu vaqtda shu instansiya so'rov qabul qilmaydi. Natija pastdagi jurnalda ko'rinadi.</p>
        </Modal>
      )}
    </div>
  )
}

export default function Instances() {
  const { data, error, reload } = useLoad(() => api.get('/portal/admin/instances'), [], { refreshMs: 10_000 })
  const items = data?.instances || []
  const sys = data?.system
  const guard = data?.memory_guard

  return (
    <>
      <div className="page-head">
        <div>
          <h1>Brauzer instansiyalari</h1>
          <p>Har instansiya holati va VNC'ga kirmasdan boshqarish. Amallar Chrome'ni qayta ishga tushirmaydi — loginlar saqlanadi. Sahifa 10 soniyada yangilanadi.</p>
        </div>
        <button type="button" className="btn btn--small" onClick={reload}>Yangilash</button>
      </div>
      <ErrorNote error={error} retry={reload} />
      {!data && !error && <Loading rows={3} />}
      {data && !data.vnc_enabled && (
        <div className="alert alert--warn" style={{ marginBottom: 16 }}>VNC sozlanmagan: serverda <code>SF_VNC_PASSWORD</code> berilmagan.</div>
      )}
      {sys?.swap_total_mb > 0 && (
        <div className={`alert ${sys.swap_percent >= 80 ? 'alert--warn' : ''}`} style={{ marginBottom: 16 }}>
          Server: swap <b>{sys.swap_percent}%</b> band ({sys.swap_used_mb} / {sys.swap_total_mb} MB), bo'sh RAM {sys.mem_available_mb} / {sys.mem_total_mb} MB.
          {guard && (guard.enabled
            ? <span className="dim"> Avto-tiklash: renderer {guard.threshold_mb} MB dan oshsa, eng kami {Math.round(guard.min_gap / 60)} daqiqa oraliq bilan, kuniga {guard.max_per_day} martagacha.</span>
            : <span className="dim"> Avto-tiklash o'chiq.</span>)}
        </div>
      )}
      {data && items.length === 0 && <Empty title="Instansiya yo'q">Klasterda CDP porti sozlanmagan.</Empty>}
      {items.length > 0 && (
        <div className="inst-grid">
          {items.map((it) => <InstanceCard key={it.port} it={it} vncEnabled={!!data.vnc_enabled} threshold={guard?.threshold_mb} onDone={reload} />)}
        </div>
      )}
      {data?.audit && (
        <div className="block panel">
          <div className="panel-head"><h2>Amallar jurnali</h2></div>
          {data.audit.length === 0 ? <Empty title="Hali amal bajarilmagan" /> : (
            <div className="table-wrap"><table className="table">
              <thead><tr><th>Vaqt</th><th>Kim</th><th>Instansiya</th><th>Amal</th><th>Natija</th></tr></thead>
              <tbody>
                {data.audit.map((a) => (
                  <tr key={a.id}>
                    <td className="muted nowrap">{fmtDateTime(a.created_at)}</td>
                    <td>{a.admin_email}</td>
                    <td>#{a.target.replace('instance:', '')}</td>
                    <td>{ACTIONS[a.action] || a.action}</td>
                    <td><span className={`tag ${a.ok ? 'tag--ok' : 'tag--bad'}`}>{a.ok ? 'OK' : 'Xato'}</span> <span className="dim small">{a.detail}</span></td>
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
