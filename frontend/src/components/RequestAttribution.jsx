import { translateText as tr, useLang } from '../i18n/index.jsx'

export default function RequestAttribution({ item, admin = false }) {
  useLang()
  const a = item.attribution || {}
  return <details className="request-details">
    <summary>{tr(a.client || tr('Noma’lum mijoz'))} · {tr(a.project || tr('Loyiha belgilanmagan'))}</summary>
    <dl className="kv">
      <dt>{tr('So‘rov ID')}</dt><dd><code>{item.request_id}</code></dd>
      <dt>{tr('API yo‘li')}</dt><dd><code>/v1/{item.endpoint}</code></dd>
      <dt>{tr('Maqsad')}</dt><dd>{tr(a.purpose || '—')}</dd>
      <dt>{tr('Chaqirilgan vositalar')}</dt><dd>{tr((a.called_tools || []).join(', ') || '—')}</dd>
      <dt>{tr('Taklif qilingan vositalar')}</dt><dd>{tr((a.requested_tools || []).join(', ') || '—')}</dd>
      <dt>{tr('Navbatda kutish')}</dt><dd>{tr(a.queue_ms == null ? '—' : `${a.queue_ms} ms`)}</dd>
      <dt>{tr('Birinchi token')}</dt><dd>{tr(item.ttfb_ms == null ? '—' : `${item.ttfb_ms} ms`)}</dd>
      {a.route_reason && <><dt>{tr('Javob bergan model')}</dt><dd><code>{a.route_model}</code> · {tr(({ input_size: 'Kontekst hajmi', provider_capacity: 'Provayder navbati', upstream_failure: 'Zaxira yo‘li' })[a.route_reason] || a.route_reason)}</dd></>}
      {a.cache_hit != null && <><dt>{tr('Javob keshi')}</dt><dd>{tr(a.cache_hit ? 'Keshdan' : 'Yangi javob')}</dd></>}
      {a.tool_protocol && <><dt>{tr('Vosita qayta so‘rovi')}</dt><dd>{tr(a.tool_protocol.final_answer ? 'Yakuniy javob' : a.tool_protocol.retried ? (a.tool_protocol.recovered ? 'Chaqiruv tiklandi' : 'Chaqiruv qaytmadi') : 'Qayta so‘ralmagan')} · {a.tool_protocol.retry_ms || 0} ms</dd></>}
      {a.reasoning_tokens != null && <><dt>{tr('Fikrlash tokenlari')}</dt><dd>{a.reasoning_tokens}</dd></>}
      {admin && <><dt>{tr('Yo‘nalish')}</dt><dd>{tr(a.provider || '—')} · {tr(a.route_model || item.model)}</dd></>}
    </dl>
  </details>
}
