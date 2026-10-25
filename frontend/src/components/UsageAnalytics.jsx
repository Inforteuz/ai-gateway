import { useState } from 'react'
import { api, qs } from '../api.js'
import { translateText as tr, useLang } from '../i18n/index.jsx'
import { fmtInt, fmtSom } from '../format.js'
import { ErrorNote, Loading, useLoad } from './ui.jsx'
import BarChart from './BarChart.jsx'

const value = (row, metric) => metric === 'cost' ? row.cost : row.prompt_tokens + row.completion_tokens

function Breakdown({ rows, metric, onSelect, selected, admin = false }) {
  const max = Math.max(1, ...rows.map(row => value(row, metric)))
  return <div className="key-analytics-bars">
    {rows.map((row, index) => {
      const label = row.name ?? row.label
      const title = label || tr('Belgilanmagan')
      const n = value(row, metric)
      const content = <>
        <span className="key-analytics-label"><b>{title}</b>{row.id != null && <small>#{row.id} · {tr(row.status === 'active' ? 'Faol' : 'Bekor qilingan')}{admin && row.email && ` · ${row.email}`}</small>}</span>
        <span className="key-analytics-track"><span style={{ width: `${n ? Math.max(1, n / max * 100) : 0}%` }} /></span>
        <span className="key-analytics-value"><b>{metric === 'cost' ? fmtSom(n) : fmtInt(n)}</b><small>{fmtInt(row.prompt_tokens)} ↑ / {fmtInt(row.completion_tokens)} ↓</small></span>
      </>
      return onSelect ? <button key={row.id} className={`key-analytics-row ${String(selected) === String(row.id) ? 'is-selected' : ''}`} onClick={() => onSelect(String(row.id))} aria-pressed={String(selected) === String(row.id)}>{content}</button>
        : <div key={`${row.label}-${index}`} className="key-analytics-row">{content}</div>
    })}
    {!rows.length && <p className="muted">{tr('Bu davrda sarf yo‘q')}</p>}
  </div>
}

export default function UsageAnalytics({ admin = false, keyId, onKeyChange }) {
  useLang()
  const [days, setDays] = useState(30)
  const [metric, setMetric] = useState('tokens')
  const [dimension, setDimension] = useState('model')
  const [owner, setOwner] = useState('')
  const { data, error, reload } = useLoad(() => api.get((admin ? '/portal/admin' : '/portal') + '/usage/analytics' + qs({ days, key_id: keyId, user_id: admin ? owner : undefined })),
    [admin, days, keyId, owner], { refreshMs: 30_000 })
  const keys = data?.keys || []
  const selected = keys.find(k => String(k.id) === String(keyId))
  const owners = [...new Map(keys.map(k => [k.user_id, k.email])).entries()].filter(([id]) => id != null)
  return <section className="panel block key-analytics" aria-label={tr('Kalitlar sarfi tahlili')}>
    <div className="panel-head"><div><h2>{tr('Kalitlar sarfi tahlili')}</h2><p className="hint">{tr('Har bir kalitning kirish va chiqish tokenlari, sarfi va ishlatilish yo‘nalishlari.')}</p></div></div>
    <div className="filter-bar">
      {admin && <select className="input" aria-label={tr('Hisob bo‘yicha filtr')} value={owner} onChange={e => { setOwner(e.target.value); onKeyChange('') }}>
        <option value="">{tr('Barcha hisoblar')}</option>{owners.map(([id, email]) => <option value={id} key={id}>{email}</option>)}
      </select>}
      <select className="input" aria-label={tr('Tahlil kaliti')} value={keyId} onChange={e => onKeyChange(e.target.value)}>
        <option value="">{tr('Barcha kalitlar')}</option>{keys.map(k => <option value={k.id} key={k.id}>{k.name} · #{k.id}{admin ? ` · ${k.email}` : ''}</option>)}
      </select>
      <select className="input" aria-label={tr('Tahlil davri')} value={days} onChange={e => setDays(Number(e.target.value))}>
        {[7, 30, 90].map(n => <option key={n} value={n}>{n} {tr('kun')}</option>)}
      </select>
      <div className="seg" role="group" aria-label={tr('Grafik o‘lchovi')}>
        {['tokens', 'cost'].map(m => <button key={m} className={metric === m ? 'is-on' : ''} onClick={() => setMetric(m)}>{tr(m === 'tokens' ? 'Tokenlar' : 'Sarf')}</button>)}
      </div>
    </div>
    <ErrorNote error={error} retry={reload} />
    {!data && !error && <Loading rows={3} />}
    {data && <>
      <div className="key-analytics-summary"><strong>{selected ? `${selected.name} · #${selected.id}` : tr('Barcha kalitlar')}</strong>
        <span>{fmtInt(data.totals.prompt_tokens)} ↑ / {fmtInt(data.totals.completion_tokens)} ↓</span><span>{fmtSom(data.totals.cost)}</span>
        {data.totals.estimated_tokens > 0 && <span className="dim">{tr('Taxminiy tokenlar')}: {fmtInt(data.totals.estimated_tokens)}</span>}
      </div>
      <div className="key-analytics-grid">
        <div><h3>{tr('Kalitlarni solishtirish')}</h3><p className="hint">{tr('Kalit ustiga bosib, uning sarfini alohida ko‘ring.')}</p>
          <Breakdown rows={keys} metric={metric} selected={keyId} onSelect={onKeyChange} admin={admin} /></div>
        <div><h3>{tr('Kunlik sarf')}</h3><BarChart items={data.daily} metric={metric} /></div>
      </div>
      <div className="panel-head"><h3>{tr('Tokenlar nimaga sarflangan?')}</h3>
        <select className="input" value={dimension} onChange={e => setDimension(e.target.value)} aria-label={tr('Sarf yo‘nalishi')}>
          {[['model', 'Model'], ['project', 'Loyiha'], ['purpose', 'Maqsad'], ['client', 'Mijoz dasturi'], ['tool', 'Vosita']].map(([id, label]) => <option key={id} value={id}>{tr(label)}</option>)}
        </select>
      </div>
      <Breakdown rows={data.dimensions[dimension] || []} metric={metric} />
      <p className="hint">{tr('Yo‘nalishlarda eng ko‘p sarflangan 50 guruh ko‘rsatiladi. Belgilanmagan ma’lumotlar alohida guruhda qoladi.')}</p>
      {dimension === 'tool' && <p className="alert alert--warn">{tr('Vosita grafigi shu vosita qatnashgan butun so‘rov tokenlarini ko‘rsatadi. Bir so‘rovda bir nechta vosita bo‘lsa, guruhlar kesishadi; ularni qo‘shib jami sarf hisoblanmaydi.')}</p>}
    </>}
  </section>
}
