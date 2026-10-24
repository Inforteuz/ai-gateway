import { translateText as tr, useLang as useLocaleRefresh } from '../../i18n/index.jsx'
import { useEffect, useState } from 'react'
import { api } from '../../api.js'
import { useSite } from '../../auth.js'
import { META_FIELD, MODEL_PLACEHOLDER, apiOrigin } from '../../site.js'
import { ErrorNote, Loading, PasswordInput, useLoad, useToast } from '../../components/ui.jsx'
import { api as _api } from '../../api.js'
import { microToSom, somToMicro, fmtNumber } from '../../format.js'
import { DEFAULT_LEVELS } from '../../reasoning.jsx'

// Maps worker error codes to operator-friendly text
const ERROR_LABELS = {
  timeout: 'Javob kelmadi (timeout)',
  empty: "Bo'sh javob",
  send_failed: 'Matn kiritib bo\'lmadi',
  quota: 'Kunlik kvota tugagan',
  login: 'Qayta login kerak',
  rate_limit: 'Vaqtinchalik to\'siq',
  provider_error: 'Provayder javob bermadi',
  degenerate_response: 'Sifatsiz javob',
}

export default function Settings() {
  useLocaleRefresh()
  const toast = useToast()
  const site = useSite()
  const apiBase = apiOrigin(site)
  const hostname = (u) => { try { return new URL(u).hostname } catch { return '' } }
  const capHostsHint = [...new Set([hostname(site.site_url), hostname(apiBase)].filter(Boolean))].join(',')
  const { data, error, reload } = useLoad(() => api.get('/portal/admin/settings'), [])
  const [f, setF] = useState(null)
  const [busy, setBusy] = useState(false)
  const [clusterStatus, setClusterStatus] = useState(null)
  const [proxyStatus, setProxyStatus] = useState(null)
  const [proxyUrl, setProxyUrl] = useState('')
  const [hook, setHook] = useState(null)

  useEffect(() => {
    if (data) setF({
      free: !!data.free_mode, registration_open: data.registration_open, bonus: String(microToSom(data.signup_bonus_micro)),
      rpm: String(data.default_rpm), def: String(data.default_max_tokens), max: String(data.max_max_tokens),
      min: String(microToSom(data.min_topup_micro)), pay: data.payment_info || '', tgChat: data.alert_telegram_chat_id || '',
      tgToken: '', tgSet: data.alert_telegram_token_set, reasoning: data.default_reasoning_effort || '',
      extBase: data.external_base_url || '', extKey: '', extSet: data.external_api_key_set,
      refModel: data.refiner_model || '', refTimeout: String(data.refine_timeout ?? 25),
      clBase: data.cluster_base_url || '', clKey: '', clSet: data.cluster_api_key_set,
      clFallback: !!data.cluster_fallback_enabled,
      fastOn: data.fastpath_enabled !== false, fastModel: data.fastpath_model || tr("qwen3:14b"),
      siteBase: data.site_base_url || '', gId: data.google_client_id || '', gSecret: '',
      gSet: data.google_client_secret_set, tgLogin: !!data.telegram_login_enabled,
      tgBot: data.telegram_login_bot || '',
      freeDaily: String(data.free_daily_token_limit ?? 0), freeModels: data.free_daily_limit_models || '',
      capOn: data.captcha_enabled !== false, capSite: data.recaptcha_site_key || '', capSecret: '',
      capSet: !!data.recaptcha_secret_set, capFails: String(data.captcha_login_after_fails ?? 0),
      capScore: String(data.captcha_min_score ?? 0.5), capHosts: data.captcha_hostnames ?? '',
      statusSince: data.status_public_since || '',
      topupTg: !!data.topup_tg_enabled, topupChat: data.topup_tg_chat_id || '', topupAdmins: data.topup_tg_admin_ids || '',
    })
  }, [data])

  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value }))

  const save = async (e) => {
    e.preventDefault(); setBusy(true)
    try {
      await api.put('/portal/admin/settings', {
        free_mode: f.free, registration_open: f.registration_open, signup_bonus_micro: somToMicro(f.bonus),
        default_rpm: Number(f.rpm), default_max_tokens: Number(f.def), max_max_tokens: Number(f.max),
        free_daily_token_limit: Number(f.freeDaily) || 0, free_daily_limit_models: f.freeModels,
        min_topup_micro: somToMicro(f.min), payment_info: f.pay, default_reasoning_effort: f.reasoning,
        alert_telegram_chat_id: f.tgChat, ...(f.tgToken ? { alert_telegram_token: f.tgToken } : {}),
        external_base_url: f.extBase, refiner_model: f.refModel, refine_timeout: Number(f.refTimeout) || 25,
        ...(f.extKey ? { external_api_key: f.extKey } : {}),
        cluster_base_url: f.clBase, ...(f.clKey ? { cluster_api_key: f.clKey } : {}),
        cluster_fallback_enabled: f.clFallback, status_public_since: f.statusSince,
        fastpath_enabled: f.fastOn, fastpath_model: f.fastModel,
        site_base_url: f.siteBase, google_client_id: f.gId, telegram_login_enabled: f.tgLogin,
        telegram_login_bot: f.tgBot, ...(f.gSecret ? { google_client_secret: f.gSecret } : {}),
        captcha_enabled: f.capOn, recaptcha_site_key: f.capSite, captcha_login_after_fails: Number(f.capFails) || 0,
        captcha_min_score: Number(f.capScore), captcha_hostnames: f.capHosts,
        ...(f.capSecret ? { recaptcha_secret: f.capSecret } : {}),
        topup_tg_enabled: f.topupTg, topup_tg_chat_id: f.topupChat, topup_tg_admin_ids: f.topupAdmins,
      })
      toast(tr("Sozlamalar saqlandi")); reload()
    } catch (err) { toast(err.message, 'error') } finally { setBusy(false) }
  }

  const testAlert = async () => {
    try { await _api.post('/portal/admin/alerts/test'); toast(tr("Sinov xabari yuborildi. Telegramni tekshiring.")) }
    catch (err) { toast(err.message, 'error') }
  }
  const hookCall = async (method, path, okText) => {
    try { setHook(await _api[method](path)); if (okText) toast(okText) }
    catch (err) { toast(err.message, 'error') }
  }
  const hookStatus = () => hookCall('get', '/portal/admin/telegram/topup-webhook')
  const hookConnect = () => hookCall('post', '/portal/admin/telegram/topup-webhook', tr("Webhook ulandi"))
  const hookRemove = () => {
    if (window.confirm(tr("Webhook uzilsinmi? Tugmalar ishlamay qoladi."))) hookCall('post', '/portal/admin/telegram/topup-webhook/remove', tr("Webhook uzildi"))
  }
  const testExternal = async () => {
    try {
      const r = await _api.post('/portal/admin/external/test')
      toast(r.refiner_found ? tr("Ulanish ishlayapti ({v0} ta model). Tekshiruvchi model topildi.", { v0: (r.models) })
                             : tr("Ulanish ishlayapti ({v0} ta model), lekin \"{v1}\" ro'yxatda yo'q.", { v0: (r.models), v1: (r.refiner_model) }),
        r.refiner_found ? 'ok' : 'error')
    } catch (err) { toast(err.message, 'error') }
  }
  const clearExternalKey = async () => {
    try { await _api.put('/portal/admin/settings', { clear_external_api_key: true }); toast(tr("Kalit o'chirildi")); reload() }
    catch (err) { toast(err.message, 'error') }
  }
  const clearToken = async () => {
    try { await _api.put('/portal/admin/settings', { clear_alert_token: true }); toast(tr("Token o'chirildi")); reload() }
    catch (err) { toast(err.message, 'error') }
  }
  const clearCaptchaSecret = async () => {
    try { await _api.put('/portal/admin/settings', { clear_recaptcha_secret: true }); toast(tr("Captcha siri o'chirildi")); reload() }
    catch (err) { toast(err.message, 'error') }
  }
  const clearGoogleSecret = async () => {
    try { await _api.put('/portal/admin/settings', { clear_google_client_secret: true }); toast(tr("Google siri o'chirildi")); reload() }
    catch (err) { toast(err.message, 'error') }
  }
  const clearClusterKey = async () => {
    try { await _api.put('/portal/admin/settings', { clear_cluster_api_key: true }); toast(tr("Kalit o'chirildi")); reload() }
    catch (err) { toast(err.message, 'error') }
  }

  const checkCluster = async () => {
    try {
      const r = await _api.get('/portal/admin/cluster/status')
      setClusterStatus(r)
      const h = r.health || {}
      toast(tr("Cluster: {v0} ta worker, {v1} ta bo'sh", { v0: (h.total_workers ?? 0), v1: (h.available_workers ?? 0) }),
            (h.total_workers ?? 0) > 0 ? 'ok' : 'error')
    } catch (err) { toast(err.message, 'error') }
  }
  const probeCluster = async () => {
    try {
      const r = await _api.post('/portal/admin/cluster/probe', {})
      const bad = Object.keys(r.problems || {}).length
      toast(bad === 0
        ? tr("Barcha {v0} ta worker sog'lom", { v0: (r.checked) })
        : tr("{v0}/{v1} sog'lom, {v2} tasida muammo: {v3}", { v0: (r.healthy), v1: (r.checked), v2: (bad), v3: (Object.entries(r.problems).map(([k, v]) => `${k} (${v})`).join(', ')) }),
        bad === 0 ? 'ok' : 'error')
      checkCluster()
    } catch (err) { toast(err.message, 'error') }
  }

  const refreshCluster = async () => {
    try {
      const r = await _api.post('/portal/admin/cluster/refresh')
      toast(tr("Qayta skanerlandi: {v0} ta worker topildi", { v0: (r.workers ?? 0) }))
      checkCluster()
    } catch (err) { toast(err.message, 'error') }
  }

  const checkProxies = async () => {
    try {
      const r = await _api.get('/portal/admin/cluster/proxies')
      setProxyStatus(r)
    } catch (err) { toast(err.message, 'error') }
  }
  const refreshProxies = async () => {
    try {
      const r = await _api.post('/portal/admin/cluster/proxies/refresh', proxyUrl ? { url: proxyUrl } : {})
      toast(tr("Yuklandi: {v0} ta proxy", { v0: (r.count ?? 0) }), r.error ? 'error' : 'ok')
      checkProxies()
    } catch (err) { toast(err.message, 'error') }
  }

  return (
    <>
      <div className="page-head"><div><h1>{tr("Sozlamalar")}</h1></div></div>
      <ErrorNote error={error} retry={reload} />
      {!f && !error && <Loading />}
      {f && (
        <form className="form" style={{ maxWidth: 680 }} onSubmit={save}>

          <div className="panel">
            <div className="panel-head"><h2>{tr("Umumiy")}</h2></div>
            <label className="field">{tr("To'lov ko'rsatmasi (foydalanuvchi \"Hisob\" sahifasida ko'radi)")} <textarea className="input" rows={4} maxLength={1500} value={f.pay} onChange={set('pay')} placeholder={tr("Masalan: Karta raqami, egasining ismi, to'lov izohiga nima yozish kerak")} />
            </label>
            <label className="field">{tr("Eng kam to'ldirish summasi, so'm")}<input className="input" type="number" min="0" value={f.min} onChange={set('min')} /></label>
            <label className="check" style={{ marginTop: 12 }}><input type="checkbox" checked={f.free} onChange={set('free')} /> {tr("Bepul rejim (hamkorlar uchun)")}</label>
            <p className="hint">{tr("Yoqilganda pul yechilmaydi va hisobda mablag' talab qilinmaydi, \"Hisob\" bo'limi va narxlar yashiriladi. Narxlar saqlanadi: o'chirsangiz, pullik rejim qaytadi.")}</p>
            <label className="check" style={{ marginTop: 8 }}><input type="checkbox" checked={f.registration_open} onChange={set('registration_open')} /> {tr("Ro'yxatdan o'tishni ochiq qilish")}</label>
            <label className="field" style={{ marginTop: 12 }}>{tr("Yangi foydalanuvchiga sovg'a, so'm")}<input className="input" type="number" min="0" value={f.bonus} onChange={set('bonus')} /><span className="hint">{tr("Faqat o'zi ro'yxatdan o'tganlarga. O'zingiz ochgan hisoblarga qo'llanmaydi.")}</span></label>
            <label className="field" style={{ marginTop: 12 }}>{tr("Standart fikrlash rejimi")} <select className="input" value={f.reasoning} onChange={set('reasoning')}>
                {DEFAULT_LEVELS.map(([v, l]) => <option key={v} value={v}>{tr(l)}</option>)}
              </select>
              <span className="hint">{tr("So'rovda ham, kalitda ham daraja ko'rsatilmasa shu qo'llanadi.")}</span>
            </label>
            <div className="form form--row" style={{ marginTop: 12 }}>
              <label className="field">{tr("Daqiqasiga so'rovlar (kalit)")}<input className="input" type="number" min="1" value={f.rpm} onChange={set('rpm')} /></label>
              <label className="field">{tr("Standart max_tokens")}<input className="input" type="number" min="1" value={f.def} onChange={set('def')} /></label>
              <label className="field">{tr("Eng katta max_tokens")}<input className="input" type="number" min="1" value={f.max} onChange={set('max')} /></label>
            </div>
            <p className="hint">{tr("max_tokens har doim cheklanadi: shu tufayli bitta so'rovning eng yuqori narxi oldindan ma'lum.")}</p>
            <div className="form form--row" style={{ marginTop: 12 }}>
              <label className="field">{tr("Bepul model: kunlik token chegarasi")}<input className="input" type="number" min="0" value={f.freeDaily} onChange={set('freeDaily')} /></label>
              <label className="field">{tr("Chegara qo'llanadigan modellar")}<input className="input" value={f.freeModels} onChange={set('freeModels')} placeholder={tr(MODEL_PLACEHOLDER)} /></label>
            </div>
            <p className="hint">{tr("Hisobiga hali depozit tushmagan foydalanuvchi shu modellarda kuniga (Toshkent vaqti) jami shuncha token ishlata oladi. Sovg'a depozit hisoblanmaydi; \"To'ldirish\" turidagi kirimdan keyin cheklov olinadi. 0 — cheksiz.")}</p>
            <label className="field" style={{ marginTop: 12 }}>{tr("Holat sahifasi hisobi boshlanadigan sana")} <input className="input" type="date" value={f.statusSince} onChange={set('statusSince')} />
              <span className="hint">{tr("Ochiq \"Holat\" sahifasidagi uptime va uzilishlar shu kundan hisoblanadi (ishga tushirish sanasi). Bo'sh — cheklovsiz.")}</span>
            </label>
          </div>

          <div className="panel">
            <div className="panel-head"><h2>{tr("Tashqi provayder")}</h2></div>
            <p className="muted small" style={{ marginBottom: 10 }}>{tr("Tekshiruvli modellar va tashqi modellar shu provayderga so'rov yuboradi. So'rov mazmuni provayderga ketishini hamkorlar bilishi kerak.")}</p>
            <label className="field">{tr("Provayder manzili (OpenAI-mos, https)")}<input className="input" value={f.extBase} onChange={set('extBase')} placeholder="https://api.openai.com/v1" /></label>
            <label className="field" style={{ marginTop: 12 }}>{tr("Provayder kaliti")} <PasswordInput autoComplete="off" value={f.extKey} onChange={set('extKey')} placeholder={f.extSet ? tr("Saqlangan (o'zgartirish uchun yangisini kiriting)") : 'sk-...'} />
            </label>
            <div className="form form--row" style={{ marginTop: 12 }}>
              <label className="field">{tr("Tekshiruvchi model(lar)")}<input className="input" value={f.refModel} onChange={set('refModel')} placeholder="model-a,model-b" /><span className="hint">{tr("Vergul bilan bir nechta yozish mumkin.")}</span></label>
              <label className="field">{tr("Tekshiruv uchun vaqt, soniya")}<input className="input" type="number" min="3" max="120" value={f.refTimeout} onChange={set('refTimeout')} /></label>
            </div>
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 14 }}>
              <button type="button" className="btn btn--small" onClick={testExternal} disabled={!f.extSet && !f.extKey}>{tr("Ulanishni sinash")}</button>
              {f.extSet && <button type="button" className="btn btn--small btn--danger" onClick={clearExternalKey}>{tr("Kalitni o'chirish")}</button>}
            </div>
          </div>

          <div className="panel panel--glow">
            <div className="panel-head">
              <h2>{tr("AI Cluster (qo'shimcha modellar)")}</h2>
              <span className="tag tag--ok">{tr("YANGI")}</span>
            </div>
            <p className="muted small" style={{ marginBottom: 10 }}> {tr("Qo'shimcha model manbaasini bitta OpenAI-mos shlyuz orqali ulaydi. Cluster xizmatini alohida ishga tushiring (")}<code>python -m cluster.server</code>{tr(") va ulanish ma'lumotlarini kiriting.")} </p>
            <label className="field">{tr("Cluster manzili")}<input className="input" value={f.clBase} onChange={set('clBase')} placeholder="http://127.0.0.1:8800" /></label>
            <label className="field" style={{ marginTop: 12 }}>{tr("Cluster kaliti (Bearer token)")} <PasswordInput autoComplete="off" value={f.clKey} onChange={set('clKey')} placeholder={f.clSet ? tr("Saqlangan (o'zgartirish uchun yangisini kiriting)") : 'cluster-token'} />
              <span className="hint">{tr("Cluster")} <code>SF_CLUSTER_API_KEY</code> {tr("env'i bilan bir xil bo'lishi kerak.")}</span>
            </label>
            <label className="check" style={{ marginTop: 14 }}>
              <input type="checkbox" checked={f.clFallback} onChange={set('clFallback')} /> {tr("Band bo'lganda zaxira modelga o'tish")} </label>
            <span className="hint" style={{ display: 'block', marginTop: 4 }}> {tr("Provayder \"high demand\" desa, so'rov o'sha provayderning zaxira modelida bajariladi. Javobda")} <code>{META_FIELD}.fallback_model: true</code> {tr("va")} <code>X-SF-Fallback-Model</code> {tr("sarlavhasi bo'ladi, hisob jurnalida")} <code>fallback_model</code> {tr("izohi yoziladi. O'chiq bo'lsa — 503 va Retry-After qaytadi.")} </span>
            <label className="check" style={{ marginTop: 14 }}>
              <input type="checkbox" checked={f.fastOn} onChange={set('fastOn')} /> {tr("Oddiy xabarlar uchun tezkor yo'l")} </label>
            <span className="hint" style={{ display: 'block', marginTop: 4 }}> {tr("Veb-chat modellariga (3.1, 3.5, 4.0) kelgan salomlashish, \"ishlayapsanmi\", \"sen kimsan\", \"rahmat\" kabi qisqa xabarlar shu GPU modelida 1–3 soniyada javob oladi (uzun tarix va tool'lar yuborilmaydi). \"ok\", \"ha\", \"davom et\" kabi buyruqlar bunga kirmaydi. Narx — so'ralgan modelniki.")} </span>
            <label className="field" style={{ marginTop: 8, maxWidth: 260 }}>{tr("Tezkor yo'l modeli (Ollama nomi)")} <input className="input" value={f.fastModel} onChange={set('fastModel')} placeholder={tr("qwen3:14b")} />
            </label>
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 14 }}>
              <button type="button" className="btn btn--small" onClick={checkCluster}>{tr("Holatni ko'rish")}</button>
              <button type="button" className="btn btn--small" onClick={refreshCluster}>{tr("Qayta skanerlash")}</button>
              <button type="button" className="btn btn--small" onClick={probeCluster}>{tr("Sessiyalarni tekshirish")}</button>
              {f.clSet && <button type="button" className="btn btn--small btn--danger" onClick={clearClusterKey}>{tr("Kalitni o'chirish")}</button>}
            </div>
            {clusterStatus && (
              <div className="metric-grid" style={{ marginTop: 14 }}>
                <div className="metric"><span className="label">{tr("Jami workerlar")}</span><b>{fmtNumber(clusterStatus.health?.total_workers ?? 0)}</b><span className="trend up">{tr("bo'sh:")} {tr(clusterStatus.health?.available_workers ?? 0)}</span></div>
                <div className="metric"><span className="label">{tr("Portlar")}</span><b>{tr(clusterStatus.health?.ports_up?.length ?? 0)}</b><span className="trend">{tr("faol")}</span></div>
                <div className="metric"><span className="label">{tr("So'nggi skaner")}</span><b style={{ fontSize: '1rem' }}>{tr(clusterStatus.health?.last_scan ? new Date(clusterStatus.health.last_scan * 1000).toLocaleTimeString() : '—')}</b></div>
              </div>
            )}
            {clusterStatus?.workers?.length > 0 && (
              <div className="cluster-grid" style={{ marginTop: 14 }}>
                {clusterStatus.workers.map((w) => (
                  <div key={`${w.browser_port}-${w.tab_id}`} className={`worker-card ${w.needs_attention ? 'is-cool' : w.cooldown_left > 0 ? 'is-cool' : w.locked ? 'is-busy' : w.available ? 'is-ok' : ''}`}>
                    <div className="head"><b>{tr(w.model)}</b><small>:{tr(w.browser_port)}</small></div>
                    <div className="state">
                      <span className={`dot ${w.needs_attention ? 'dot--off' : w.available ? 'dot--on' : 'dot--off'}`} />
                      {tr(w.needs_attention === 'login' ? tr("Login kerak")
                        : w.needs_attention === 'quota' ? 'Kvota tugagan'
                        : w.cooldown_left > 0 ? `Cooldown ${w.cooldown_left}s`
                        : w.locked ? 'Band'
                        : w.available ? tr("Bo'sh") : 'Nofaol')}
                    </div>
                    <div className="foot">
                      <span>{tr(w.total_requests)} {tr("so'rov")}</span>
                      <span>{Math.round((w.health_score ?? 1) * 100)}{tr("% sog'lom")}</span>
                    </div>
                    {w.last_error && (
                      <div className="worker-err" title={tr(w.last_error)}>{ERROR_LABELS[w.last_error] || w.last_error}</div>
                    )}
                  </div>
                ))}
              </div>
            )}

            <h3 style={{ marginTop: 20, fontSize: '1rem' }}>{tr("Proxy pool")}</h3>
            <p className="muted small">{tr("So'rovlar ba'zan mijoz IP'si, ba'zan proxy orqali ketishi uchun. Webshare va boshqa provayderlardan avtomatik yuklab olish mumkin.")}</p>
            <label className="field" style={{ marginTop: 8 }}>{tr("Proxy ro'yxati URL'i (ixtiyoriy, saqlanmaydi)")} <input className="input" value={proxyUrl} onChange={(e) => setProxyUrl(e.target.value)} placeholder="https://proxy-provider.example/list.txt" />
              <span className="hint">{tr("Bo'sh qoldirsangiz, cluster o'z")} <code>SF_CLUSTER_PROXY_URL</code> {tr("env'ini ishlatadi.")}</span>
            </label>
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 14 }}>
              <button type="button" className="btn btn--small" onClick={checkProxies}>{tr("Holatni ko'rish")}</button>
              <button type="button" className="btn btn--small" onClick={refreshProxies}>{tr("Hoziroq yuklab olish")}</button>
            </div>
            {proxyStatus && (
              <div className="metric-grid" style={{ marginTop: 14 }}>
                <div className="metric"><span className="label">{tr("Jami proxylar")}</span><b>{fmtNumber(proxyStatus.summary?.total ?? 0)}</b></div>
                <div className="metric"><span className="label">{tr("Direct slotlar")}</span><b>{tr(proxyStatus.direct_slots ?? 0)}</b><span className="trend">{tr("proxy'siz ishlaydi")}</span></div>
                <div className="metric"><span className="label">{tr("Avtomatik yangilash")}</span><b style={{ fontSize: '1rem' }}>{tr(proxyStatus.refresh_interval ? `${Math.round(proxyStatus.refresh_interval / 60)} daq` : tr("o'chiq"))}</b></div>
              </div>
            )}
          </div>

          <div className="panel">
            <div className="panel-head"><h2>{tr("Google va Telegram orqali kirish")}</h2></div>
            <p className="muted small" style={{ marginBottom: 10 }}> {tr("Kalitlar bo'sh bo'lsa, kirish sahifasida tugma umuman ko'rsatilmaydi. Yangi hisob ochilganda sovg'a va bir IP cheklovlari oddiy ro'yxatdan o'tishdagi kabi ishlaydi.")} </p>
            <label className="field">{tr("Saytning tashqi manzili")} <input className="input" value={f.siteBase} onChange={set('siteBase')} placeholder={tr(apiBase)} />
              <span className="hint">{tr("Google'ga yuboriladigan qaytish manzili shundan yasaladi.")}</span>
            </label>

            <h3 style={{ marginTop: 20, fontSize: '1rem' }}>{tr("Google")}</h3>
            <p className="muted small"> {tr("Google Cloud konsolida \"OAuth 2.0 Client ID\" (Web application) yarating va quyidagi qaytish manzilini \"Authorized redirect URIs\" ro'yxatiga qo'shing:")} </p>
            <div className="keybox" style={{ marginTop: 8 }}>
              <code>{(f.siteBase || apiBase).replace(/\/$/, '')}/portal/auth/google/callback</code>
            </div>
            <label className="field" style={{ marginTop: 12 }}>Client ID
              <input className="input" autoComplete="off" value={f.gId} onChange={set('gId')} placeholder={tr("...apps.googleusercontent.com")} />
            </label>
            <label className="field" style={{ marginTop: 12 }}>Client secret
              <PasswordInput autoComplete="off" value={f.gSecret} onChange={set('gSecret')} placeholder={f.gSet ? tr("Saqlangan (o'zgartirish uchun yangisini kiriting)") : 'GOCSPX-...'} />
              <span className="hint">{tr("Ikkisi ham to'ldirilmaguncha Google tugmasi ko'rinmaydi.")}</span>
            </label>
            {f.gSet && (
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 14 }}>
                <button type="button" className="btn btn--small btn--danger" onClick={clearGoogleSecret}>{tr("Sirni o'chirish")}</button>
              </div>
            )}

            <h3 style={{ marginTop: 20, fontSize: '1rem' }}>{tr("Telegram")}</h3>
            <p className="muted small"> {tr("Xabarnomalar uchun ishlatiladigan botning o'zi yetarli. Lekin AVVAL BotFather'da")} <code>/setdomain</code> {tr("buyrug'i bilan saytning domenini botga biriktiring — aks holda tugma o'rnida \"Bot domain invalid\" chiqadi. Domenni biriktirgandan keyingina quyidagi belgini yoqing.")} </p>
            <label className="check" style={{ marginTop: 10 }}>
              <input type="checkbox" checked={f.tgLogin} onChange={set('tgLogin')} /> {tr("Telegram orqali kirishni yoqish")} </label>
            <label className="field" style={{ marginTop: 12 }}>{tr("Bot username (ixtiyoriy)")} <input className="input" value={f.tgBot} onChange={set('tgBot')} placeholder="my_bot" />
              <span className="hint">{tr("Bo'sh qoldirsangiz, bot tokeni orqali o'zi aniqlanadi.")}</span>
            </label>
          </div>

          <div className="panel">
            <div className="panel-head">
              <h2>{tr("Captcha (Google reCAPTCHA v3)")}</h2>
              <span className={`tag ${data.captcha_active ? 'tag--ok' : ''}`}>{tr(data.captcha_active ? 'ishlayapti' : tr("o'chiq"))}</span>
            </div>
            <p className="muted small" style={{ marginBottom: 10 }}> {tr("Kirish, ro'yxatdan o'tish va parolni tiklashni botlardan himoya qiladi. Ko'rinmas: foydalanuvchiga hech narsa ko'rsatilmaydi, Google har so'rovga 0.0 (bot) … 1.0 (odam) ball beradi. Ikkala kalit ham kiritilmaguncha captcha o'chiq turadi. Kalitlar: google.com/recaptcha/admin (turi — v3).")} </p>
            {data.recaptcha_env_keys && (
              <p className="hint">{tr("Kalitlar serverdagi env fayldan olinmoqda — quyidagi maydonlardan ustun.")}</p>
            )}
            {data.captcha_env_disabled && (
              <p className="form-error">{tr("Serverda SF_CAPTCHA=0 — captcha favqulodda o'chirilgan.")}</p>
            )}
            <label className="check" style={{ marginTop: 10 }}>
              <input type="checkbox" checked={f.capOn} onChange={set('capOn')} /> {tr("Captchani yoqish")} </label>
            <label className="field" style={{ marginTop: 12 }}>Site key (ochiq)
              <input className="input" autoComplete="off" value={f.capSite} onChange={set('capSite')} placeholder={tr("6L...")} />
            </label>
            <label className="field" style={{ marginTop: 12 }}>Secret key
              <PasswordInput autoComplete="off" value={f.capSecret} onChange={set('capSecret')} placeholder={f.capSet ? tr("Saqlangan (o'zgartirish uchun yangisini kiriting)") : tr("6L...")} />
            </label>
            <label className="field" style={{ marginTop: 12 }}>{tr("Minimal ball (0–1)")} <input className="input" type="number" min="0" max="1" step="0.1" value={f.capScore} onChange={set('capScore')} />
              <span className="hint">{tr("Google tavsiyasi — 0.5. Ko'tarsangiz botlar kamroq o'tadi, lekin VPN'dagi odamlar ham rad etilishi mumkin.")}</span>
            </label>
            <label className="field" style={{ marginTop: 12 }}>{tr("Ruxsat etilgan domenlar")} <input className="input" autoComplete="off" value={f.capHosts} onChange={set('capHosts')} placeholder={tr(capHostsHint)} />
              <span className="hint">{tr("Token faqat shu saytlarda olingan bo'lsa qabul qilinadi. Bo'sh — tekshirilmaydi.")}</span>
            </label>
            <label className="field" style={{ marginTop: 12 }}>{tr("Kirishda captcha qachon so'ralsin")} <select className="input" value={f.capFails} onChange={set('capFails')}>
                <option value="0">{tr("Har doim")}</option>
                {[1, 2, 3, 5].map((n) => <option key={n} value={String(n)}>{tr(n)} {tr("ta xato urinishdan keyin")}</option>)}
              </select>
              <span className="hint">{tr("Ro'yxatdan o'tish va parolni tiklashda har doim so'raladi.")}</span>
            </label>
            {f.capSet && (
              <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 14 }}>
                <button type="button" className="btn btn--small btn--danger" onClick={clearCaptchaSecret}>{tr("Sirni o'chirish")}</button>
              </div>
            )}
          </div>

          <div className="panel">
            <div className="panel-head"><h2>{tr("Telegram xabarnomalari")}</h2></div>
            <p className="muted small">{tr("Model uzilganda va qayta ishlaganda, hamda har kuni soat 9:00 da (Toshkent) hisobot yuboriladi.")}</p>
            <label className="field" style={{ marginTop: 10 }}>{tr("Bot tokeni")} <PasswordInput autoComplete="off" value={f.tgToken} onChange={set('tgToken')} placeholder={f.tgSet ? tr("Saqlangan (o'zgartirish uchun yangisini kiriting)") : '123456:ABC...'} />
            </label>
            <label className="field" style={{ marginTop: 12 }}>Chat ID<input className="input" value={f.tgChat} onChange={set('tgChat')} placeholder={tr("Masalan: 123456789 yoki -1001234567890")} /></label>
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 14 }}>
              <button type="button" className="btn btn--small" onClick={testAlert} disabled={!f.tgSet && !f.tgToken}>{tr("Sinov xabarini yuborish")}</button>
              {f.tgSet && <button type="button" className="btn btn--small btn--danger" onClick={clearToken}>{tr("Tokenni o'chirish")}</button>}
            </div>
            <p className="hint" style={{ marginTop: 8 }}>{tr("Sinov saqlangan sozlamalar bilan yuboriladi: avval \"Saqlash\" ni bosing.")}</p>
          </div>

          <div className="panel">
            <div className="panel-head">
              <h2>{tr("To'ldirish so'rovlari Telegramda")}</h2>
              <span className={`tag ${data.topup_tg_enabled && data.topup_tg_webhook_set ? 'tag--ok' : ''}`}>
                {tr(data.topup_tg_enabled && data.topup_tg_webhook_set ? 'ishlayapti' : tr("o'chiq"))}
              </span>
            </div>
            <p className="muted small"> {tr("Har bir yangi so'rov \"Tasdiqlash\" / \"Rad etish\" tugmalari bilan Telegramga keladi. Tasdiqlash — \"To'ldirish\" sahifasidagi bilan bir xil: hisobga yoziladi, referal mukofoti hisoblanadi. Kirish boti (Telegram orqali kirish tokeni) ishlatiladi — ogohlantirish boti emas. Botga avval o'zingiz /start yozing, aks holda u sizga xabar yubora olmaydi.")} </p>
            <label className="check" style={{ marginTop: 10 }}>
              <input type="checkbox" checked={f.topupTg} onChange={set('topupTg')} /> {tr("Telegramga yuborishni yoqish")} </label>
            <label className="field" style={{ marginTop: 12 }}>{tr("Chat ID (ixtiyoriy)")} <input className="input" value={f.topupChat} onChange={set('topupChat')} placeholder={f.tgChat || '123456789'} />
              <span className="hint">{tr("Bo'sh bo'lsa — \"Telegram xabarnomalari\" dagi Chat ID.")}</span>
            </label>
            <label className="field" style={{ marginTop: 12 }}>{tr("Tugmani bosa oladiganlar (Telegram user ID, vergul bilan)")} <input className="input" value={f.topupAdmins} onChange={set('topupAdmins')} placeholder={tr("Bo'sh — shaxsiy chat egasi")} />
              <span className="hint">{tr("Boshqa birov bossa — rad etiladi. Guruhga yuborilsa, bu ro'yxat majburiy.")}</span>
            </label>
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginTop: 14 }}>
              <button type="button" className="btn btn--small" onClick={hookConnect}>{tr("Webhook'ni ulash")}</button>
              <button type="button" className="btn btn--small" onClick={hookStatus}>{tr("Holatini tekshirish")}</button>
              {data.topup_tg_webhook_set && <button type="button" className="btn btn--small btn--danger" onClick={hookRemove}>{tr("Webhook'ni uzish")}</button>}
            </div>
            {hook && (
              <p className="hint" style={{ marginTop: 8 }}> {tr("Bot:")} {tr(hook.bot ? `@${hook.bot}` : 'aniqlanmadi')} {tr("· Manzil:")} {tr(hook.url || 'ulanmagan')}
                {tr(hook.url && hook.url !== hook.expected_url && ` (kutilgan: ${hook.expected_url})`)}
                {tr(hook.last_error && tr("· Oxirgi xato: {v0}", { v0: (hook.last_error) }))}
                {` · Ruxsat: ${hook.admin_ids.length ? hook.admin_ids.join(', ') : tr("hech kim")}`}
              </p>
            )}
            <p className="hint" style={{ marginTop: 8 }}>{tr("Ulashdan oldin \"Saqlash\" ni bosing: webhook saytning tashqi manzili (https) bilan ulanadi.")}</p>
          </div>

          <div><button className="btn btn--primary" disabled={busy}>{tr("Saqlash")}</button></div>
        </form>
      )}
    </>
  )
}
