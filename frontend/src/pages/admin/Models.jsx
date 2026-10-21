import { useState } from 'react'
import { api } from '../../api.js'
import { useAuth, useSite } from '../../auth.js'
import { Empty, ErrorNote, Loading, Modal, useLoad, useToast } from '../../components/ui.jsx'
import { fmtNumber, microToSom, somToMicro } from '../../format.js'

const LEVELS = [['', 'Umumiy sozlama bo\'yicha'], ['none', 'O\'chiq'], ['low', 'Past (low)'], ['medium', 'O\'rta (medium)'], ['high', 'Yuqori (high)'], ['model', 'Modelning o\'zi hal qiladi']]

function Row({ m, others, onSaved }) {
  const toast = useToast()
  const initial = {
    display_name: m.display_name, description: m.description || '', price_in: String(microToSom(m.price_in_per_1m)), price_out: String(microToSom(m.price_out_per_1m)),
    context_window: String(m.context_window), enabled: m.enabled, listed: m.listed, refine: m.refine,
    default_reasoning: m.default_reasoning || '', fallback_price_from: m.fallback_price_from || '', allow_thinking: m.allow_thinking,
  }
  const [f, setF] = useState(initial)
  const [busy, setBusy] = useState(false)
  const dirty = JSON.stringify(f) !== JSON.stringify(initial)
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value }))
  const save = async () => {
    setBusy(true)
    try {
      await api.patch(`/portal/admin/models/${encodeURIComponent(m.id)}`, {
        display_name: f.display_name, description: f.description, price_in_per_1m: somToMicro(f.price_in), price_out_per_1m: somToMicro(f.price_out),
        context_window: Number(f.context_window), enabled: f.enabled, listed: f.listed, refine: f.refine,
        default_reasoning: f.default_reasoning || null, fallback_price_from: f.fallback_price_from || null, allow_thinking: f.allow_thinking,
      })
      toast('Saqlandi. O\'zgarish keyingi so\'rovlardan amal qiladi.'); onSaved()
    } catch (err) { toast(err.message, 'error') } finally { setBusy(false) }
  }
  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div style={{ display: 'flex', gap: 10, alignItems: 'baseline', flexWrap: 'wrap', marginBottom: 14 }}>
        <h3>{m.id}</h3>
        <span className={`tag ${m.provider === 'external' ? 'tag--warn' : m.provider === 'cluster' ? 'tag--ok' : ''}`}>
          {m.provider === 'external' ? 'tashqi provayder' : m.provider === 'cluster' ? 'cluster' : 'mahalliy'}
        </span>
        {m.refine && <span className="tag tag--ok">tekshiruvli</span>}
        {m.provider === 'local' && !m.allow_thinking && <span className="tag">o'ylash o'chiq</span>}
        {!m.listed && <span className="tag">ro'yxatda yo'q</span>}
        {!m.enabled && <span className="tag tag--bad">o'chiq</span>}
        <span className="dim small">ichki nom (mijozga ko'rinmaydi): {m.upstream_name}</span>
      </div>
      <div className="form form--row" style={{ alignItems: 'end' }}>
        <label className="field">Mijozga ko'rinadigan nom<input className="input" value={f.display_name} onChange={set('display_name')} maxLength={80} /></label>
        <label className="field">Kirish, so'm / 1M<input className="input" type="number" min="0" step="any" value={f.price_in} onChange={set('price_in')} /></label>
        <label className="field">Chiqish, so'm / 1M<input className="input" type="number" min="0" step="any" value={f.price_out} onChange={set('price_out')} /></label>
        <label className="field">Kontekst, token<input className="input" type="number" min="256" value={f.context_window} onChange={set('context_window')} /></label>
      </div>
      <div className="form form--row" style={{ marginTop: 14, alignItems: 'end' }}>
        <label className="field">Tavsif<input className="input" value={f.description} onChange={set('description')} maxLength={300} /></label>
        {m.provider === 'local' && f.allow_thinking && (
          <label className="field">Standart fikrlash
            <select className="input" value={f.default_reasoning} onChange={set('default_reasoning')}>{LEVELS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}</select>
          </label>
        )}
        {m.refine && (
          <label className="field">Tekshiruv ishlamasa, narx shu modeldan
            <select className="input" value={f.fallback_price_from} onChange={set('fallback_price_from')}>
              <option value="">O'zining narxi</option>
              {others.map((o) => <option key={o.id} value={o.id}>{o.id}</option>)}
            </select>
          </label>
        )}
      </div>
      <div style={{ display: 'flex', gap: 22, flexWrap: 'wrap', alignItems: 'center', marginTop: 16 }}>
        <label className="check"><input type="checkbox" checked={f.enabled} onChange={set('enabled')} /> Yoqilgan</label>
        <label className="check"><input type="checkbox" checked={f.listed} onChange={set('listed')} /> Ro'yxatda ko'rinadi</label>
        {m.provider === 'local' && <label className="check"><input type="checkbox" checked={f.refine} onChange={set('refine')} /> Javob tashqi model bilan tekshiriladi</label>}
        {m.provider === 'local' && <label className="check"><input type="checkbox" checked={f.allow_thinking} onChange={set('allow_thinking')} /> O'ylash (reasoning) yoqilishi mumkin</label>}
        <button className="btn btn--primary btn--small" style={{ marginLeft: 'auto' }} disabled={!dirty || busy} onClick={save}>Saqlash</button>
      </div>
    </section>
  )
}

const PROVIDER_PATH = {
  local: '/portal/admin/upstream-models',
  external: '/portal/admin/external/models',
  cluster: '/portal/admin/cluster/models',
}

function AddModel({ models, onClose, onDone }) {
  const toast = useToast()
  const family = useSite().assistant_name
  const familyId = family.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'model'
  const [provider, setProvider] = useState('local')
  const source = useLoad(() => api.get(PROVIDER_PATH[provider]), [provider])
  const [pick, setPick] = useState('')
  const [f, setF] = useState({ id: '', display_name: '', description: '', price_in: '10000', price_out: '30000', context_window: '16384', refine: false, listed: true, fallback: '', allow_thinking: false })
  const set = (k) => (e) => setF((x) => ({ ...x, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value }))
  const save = async (e) => {
    e.preventDefault()
    try {
      await api.post('/portal/admin/models', {
        id: f.id.trim(), upstream_name: pick, display_name: f.display_name.trim(), description: f.description, provider,
        price_in_per_1m: somToMicro(f.price_in), price_out_per_1m: somToMicro(f.price_out), context_window: Number(f.context_window),
        enabled: true, listed: f.listed, refine: provider === 'local' && f.refine, fallback_price_from: f.fallback || null, allow_thinking: provider === 'local' && f.allow_thinking,
      })
      toast('Model qo\'shildi'); onDone(); onClose()
    } catch (err) { toast(err.message, 'error') }
  }
  return (
    <Modal title="Yangi model qo'shish" wide onClose={onClose} actions={<>
      <button className="btn" onClick={onClose}>Bekor qilish</button>
      <button className="btn btn--primary" form="add-model" disabled={!pick || !f.id || !f.display_name}>Qo'shish</button>
    </>}>
      <div className="seg" role="group" aria-label="Manba">
        <button type="button" className={provider === 'local' ? 'is-on' : ''} onClick={() => { setProvider('local'); setPick('') }}>Mahalliy (ishxona)</button>
        <button type="button" className={provider === 'external' ? 'is-on' : ''} onClick={() => { setProvider('external'); setPick('') }}>Tashqi provayder</button>
        <button type="button" className={provider === 'cluster' ? 'is-on' : ''} onClick={() => { setProvider('cluster'); setPick('') }}>AI Cluster</button>
      </div>
      <ErrorNote error={source.error} />
      {!source.data && !source.error && <Loading rows={2} />}
      {source.data && (
        <form id="add-model" className="form" onSubmit={save}>
          <div>
            <p className="hint" style={{ marginBottom: 8 }}>
              {provider === 'cluster' ? 'Cluster kanalini tanlang (mijozga ko\'rinmaydi):' : 'Haqiqiy modelni tanlang (mijozga ko\'rsatilmaydi):'}
            </p>
            <div className="presets">{source.data.items.map((m) => (
              <button type="button" key={m.name} className={`btn btn--small ${pick === m.name ? 'btn--primary' : ''}`} onClick={() => setPick(m.name)}>
                {m.name}
                {provider === 'cluster' && m.active_workers > 0 && <span style={{ marginLeft: 6, opacity: 0.75, fontSize: '0.78em' }}>({m.available_workers}/{m.active_workers})</span>}
                {m.added ? ' ✓' : ''}
              </button>
            ))}</div>
            {provider === 'cluster' && source.data.items.length === 0 && (
              <p className="muted small" style={{ marginTop: 8 }}>Hech qanday faol worker topilmadi. Cluster shlyuzini ishga tushiring va Chromium tablarini oching.</p>
            )}
          </div>
          <div className="form form--row">
            <label className="field">Mijozlar ishlatadigan id<input className="input" required value={f.id} onChange={set('id')} placeholder={`${familyId}-1.0`} pattern="[A-Za-z0-9._:/-]+" /></label>
            <label className="field">Ko'rinadigan nom<input className="input" required value={f.display_name} onChange={set('display_name')} placeholder={`${family} 1.0`} /></label>
          </div>
          <label className="field">Tavsif<input className="input" value={f.description} onChange={set('description')} maxLength={300} /></label>
          <div className="form form--row">
            <label className="field">Kirish, so'm / 1M<input className="input" type="number" min="0" step="any" value={f.price_in} onChange={set('price_in')} /></label>
            <label className="field">Chiqish, so'm / 1M<input className="input" type="number" min="0" step="any" value={f.price_out} onChange={set('price_out')} /></label>
            <label className="field">Kontekst, token<input className="input" type="number" min="256" value={f.context_window} onChange={set('context_window')} /></label>
          </div>
          {provider === 'local' && (
            <>
              <label className="check"><input type="checkbox" checked={f.refine} onChange={set('refine')} /> Javob tashqi model bilan tekshiriladi (tekshiruvli)</label>
              {f.refine && (
                <label className="field">Tekshiruv ishlamasa, narx shu modeldan
                  <select className="input" value={f.fallback} onChange={set('fallback')}><option value="">O'zining narxi</option>{models.map((o) => <option key={o.id} value={o.id}>{o.id}</option>)}</select>
                </label>
              )}
            </>
          )}
          {provider === 'local' && <label className="check"><input type="checkbox" checked={f.allow_thinking} onChange={set('allow_thinking')} /> O'ylash (reasoning) yoqilishi mumkin: sekin va ko'p token sarflaydi</label>}
          <label className="check"><input type="checkbox" checked={f.listed} onChange={set('listed')} /> Mijozlar ro'yxatida ko'rinsin</label>
          {(provider === 'external' || provider === 'cluster' || f.refine) && (
            <div className="alert alert--warn">
              Bu modelda so'rov mazmuni tashqi manbaga yuboriladi. Mijoz hujjatlarida "uchinchi tomon ishtirokida" deb ko'rsatiladi.
            </div>
          )}
        </form>
      )}
    </Modal>
  )
}

export default function Models() {
  const { freeMode } = useAuth()
  const { data, error, reload } = useLoad(() => api.get('/portal/admin/models'), [])
  const [adding, setAdding] = useState(false)
  const items = data?.items || []
  return (
    <>
      <div className="page-head">
        <div>
          <h1>Modellar va narxlar</h1>
          <p>Mijozlar faqat bu yerdagi nom va tavsifni ko'radi. Haqiqiy (ichki) model nomi API javoblarida, xatolarda va oqimda yashiriladi. O'zgarish keyingi so'rovlardan boshlab amal qiladi.</p>
        </div>
        <button className="btn btn--primary" onClick={() => setAdding(true)}>Yangi model</button>
      </div>
      {freeMode && <div className="alert alert--ok" style={{ marginBottom: 18 }}>Bepul rejim yoqilgan: narxlar hozir hisobga olinmaydi, lekin saqlanadi (pullik rejimga qaytsangiz ishlatiladi).</div>}
      <ErrorNote error={error} retry={reload} />
      {!data && !error && <Loading />}
      {data && items.length === 0 && <Empty title="Model yo'q">Yangi model qo'shing.</Empty>}
      {items.map((m) => <Row key={m.id + JSON.stringify([m.price_in_per_1m, m.price_out_per_1m, m.enabled, m.listed, m.refine, m.default_reasoning, m.fallback_price_from, m.display_name, m.allow_thinking])} m={m} others={items.filter((o) => o.id !== m.id)} onSaved={reload} />)}
      <p className="hint" style={{ marginTop: 6 }}>Misol: 10 000 so'm / 1M degani 1 000 token = {fmtNumber(10_000 * 1000 / 1_000_000, 2)} so'm.</p>
      {adding && <AddModel models={items} onClose={() => setAdding(false)} onDone={reload} />}
    </>
  )
}
