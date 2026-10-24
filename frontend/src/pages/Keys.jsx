import { useState } from 'react'
import { api } from '../api.js'
import { useAuth, useSite } from '../auth.js'
import { apiOrigin } from '../site.js'
import { CodeBlock, CopyButton, CopyIcon, Empty, ErrorNote, Loading, Modal, PageHead, useLoad, useToast } from '../components/ui.jsx'
import { IconCheck, IconClock, IconLayers, IconLink, IconPlus, IconShield, IconZap } from '../components/icons.jsx'
import { fmtAgo, fmtDateTime, fmtSom, microToSom, somToMicro } from '../format.js'
import { LevelSelect, levelShort } from '../reasoning.jsx'
import { useT } from '../i18n/index.jsx'

function KeyReveal({ item }) {
  // The key is also kept encrypted (see security.encrypt_secret), so its owner can read
  // it back instead of having to revoke and re-issue after losing the one copy they saw.
  const t = useT()
  const toast = useToast()
  const [full, setFull] = useState('')
  const [busy, setBusy] = useState(false)
  const masked = `${item.prefix}${'•'.repeat(14)}${item.last4}`

  async function show() {
    if (full) { setFull(''); return }
    setBusy(true)
    try {
      const { key } = await api.get(`/portal/keys/${item.id}/reveal`)
      if (key) setFull(key)
      else toast(t('keys.revealNone'))      // made before this existed, or no SF_KEY_SECRET
    } catch (e) {
      toast(e.message || t('keys.revealNone'))
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <div className="copy-row">
        <div className="who"><b className="mono">{full || masked}</b></div>
        {full && <CopyIcon text={full} title={t('keys.apiKey')} />}
      </div>
      <div className="hstack" style={{ gap: 8, marginTop: 8 }}>
        <button className="btn btn--small" onClick={show} disabled={busy}>
          {full ? t('keys.hide') : t('keys.reveal')}
        </button>
      </div>
      <span className="dim small" style={{ marginTop: 6, display: 'block' }}>
        {t('keys.onceNote')}
      </span>
    </>
  )
}

function CreateKey({ onClose, onDone }) {
  const { freeMode } = useAuth()
  const t = useT()
  const [name, setName] = useState('')
  const [limit, setLimit] = useState('')
  const [effort, setEffort] = useState('inherit')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [created, setCreated] = useState(null)
  const origin = apiOrigin(useSite())

  const submit = async (e) => {
    e.preventDefault(); setBusy(true); setError('')
    try {
      const r = await api.post('/portal/keys', { name, monthly_limit: limit ? somToMicro(limit) : null, reasoning_effort: effort })
      setCreated(r.key); onDone()
    } catch (err) { setError(err.message) } finally { setBusy(false) }
  }

  if (created) {
    return (
      <Modal title={t('keys.readyTitle')} onClose={onClose} actions={<button className="btn btn--primary" onClick={onClose}>{t('keys.savedClose')}</button>}>
        <div className="alert alert--warn">{t('keys.readyWarn')}</div>
        <div className="keybox"><code>{created}</code><CopyButton text={created} small={false} /></div>
        <CodeBlock code={`curl ${origin}/v1/models \\\n  -H "Authorization: Bearer ${created}"`} />
      </Modal>
    )
  }
  return (
    <Modal title={t('keys.newTitle')} onClose={onClose} actions={<>
      <button className="btn" onClick={onClose}>{t('common.cancel')}</button>
      <button className="btn btn--primary" form="create-key" disabled={busy || !name.trim()}>{busy ? t('keys.creating') : t('keys.create')}</button>
    </>}>
      <form id="create-key" className="form" onSubmit={submit}>
        {error && <div className="alert alert--error" role="alert">{error}</div>}
        <label className="field">{t('keys.name')}
          <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder={t('keys.namePh')} maxLength={60} autoFocus required />
          <span className="hint">{t('keys.nameHint')}</span>
        </label>
        {!freeMode && (
          <label className="field">{t('keys.limitOptional')}
            <input className="input" type="number" min="0" step="1" value={limit} onChange={(e) => setLimit(e.target.value)} placeholder={t('keys.unlimited')} />
            <span className="hint">{t('keys.limitHint')}</span>
          </label>
        )}
        <label className="field">{t('keys.reasoning')}
          <LevelSelect value={effort} onChange={setEffort} />
          <span className="hint">{t('keys.reasoningHint')}</span>
        </label>
      </form>
    </Modal>
  )
}

function EditKey({ item, onClose, onDone }) {
  const { freeMode } = useAuth()
  const toast = useToast()
  const t = useT()
  const [name, setName] = useState(item.name)
  const [limit, setLimit] = useState(item.monthly_limit != null ? String(microToSom(item.monthly_limit)) : '')
  const [effort, setEffort] = useState(item.reasoning_effort || 'inherit')
  const [busy, setBusy] = useState(false)
  const save = async (e) => {
    e.preventDefault(); setBusy(true)
    try {
      await api.patch(`/portal/keys/${item.id}`, { name, monthly_limit: limit === '' ? null : somToMicro(limit), clear_monthly_limit: limit === '', reasoning_effort: effort })
      toast(t('common.saved')); onDone(); onClose()
    } catch (err) { toast(err.message, 'error') } finally { setBusy(false) }
  }
  return (
    <Modal title={t('keys.editTitle')} onClose={onClose} actions={<>
      <button className="btn" onClick={onClose}>{t('common.cancel')}</button>
      <button className="btn btn--primary" form="edit-key" disabled={busy}>{t('common.save')}</button>
    </>}>
      <form id="edit-key" className="form" onSubmit={save}>
        <label className="field">{t('keys.name')}<input className="input" value={name} onChange={(e) => setName(e.target.value)} maxLength={60} required /></label>
        {!freeMode && (
          <label className="field">{t('keys.limit')}
            <input className="input" type="number" min="0" value={limit} onChange={(e) => setLimit(e.target.value)} placeholder={t('keys.unlimited')} />
          </label>
        )}
        <label className="field">{t('keys.reasoning')}
          <LevelSelect value={effort} onChange={setEffort} />
          <span className="hint">{t('keys.reasoningHint')}</span>
        </label>
      </form>
    </Modal>
  )
}

export default function Keys() {
  const { freeMode } = useAuth()
  const site = useSite()
  const toast = useToast()
  const t = useT()
  const { data, error, loading, reload } = useLoad(() => api.get('/portal/keys'), [])
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState(null)
  const [revoking, setRevoking] = useState(null)

  const revoke = async () => {
    try { await api.post(`/portal/keys/${revoking.id}/revoke`); toast(t('keys.revokedToast')); reload() }
    catch (err) { toast(err.message, 'error') }
    setRevoking(null)
  }
  const items = data?.items || []
  const active = items.filter((k) => k.status === 'active')
  const revoked = items.filter((k) => k.status !== 'active')
  const baseUrl = `${apiOrigin(site)}/v1`

  return (
    <>
      <PageHead
        eyebrow={t('keys.eyebrow')}
        title={[t('keys.title1'), t('keys.title2')]}
        lead={t('keys.lead')}
        action={<button className="btn btn--primary btn--large" onClick={() => setCreating(true)}><IconPlus /> {t('keys.new')}</button>}
      />
      <ErrorNote error={error} retry={reload} />
      {loading && !data && <Loading />}
      {data && active.length === 0 && (
        <Empty title={t('keys.emptyTitle')}><span>{t('keys.emptyText')}</span><button className="btn btn--primary" onClick={() => setCreating(true)}>{t('keys.createFirst')}</button></Empty>
      )}

      {active.map((k) => {
        const used = k.monthly_limit ? Math.min(100, Math.round((k.month_spent / k.monthly_limit) * 100)) : null
        return (
          <section className="panel key-card" key={k.id}>
            <div className="panel-head">
              <div className="panel-head__t">
                <div className="hstack" style={{ gap: 10 }}>
                  <h2>{k.name}</h2>
                  <span className="tag tag--ok"><IconCheck /> {t('keys.active')}</span>
                  {k.free && <span className="tag tag--warn">{t('keys.freeTag')}</span>}
                </div>
                <p>
                  {t('keys.reasoningShort', { level: k.reasoning_effort === 'inherit' ? t('keys.inherit') : levelShort(t, k.reasoning_effort) })}
                  {k.created_at ? <> &middot; {t('keys.createdAt', { date: fmtDateTime(k.created_at) })}</> : null}
                </p>
              </div>
              <div className="hstack" style={{ gap: 8 }}>
                <button className="btn btn--small" onClick={() => setEditing(k)}>{t('keys.edit')}</button>
                <button className="btn btn--small btn--danger" onClick={() => setRevoking(k)}>{t('keys.revoke')}</button>
              </div>
            </div>

            <div className="key-grid">
              <div className="mini-card">
                <span className="mini-card__label">{t('keys.apiKey')}</span>
                <KeyReveal item={k} />
              </div>
              {!freeMode && (
                <div className="mini-card mini-card--mint">
                  <div className="hstack" style={{ gap: 12, flexWrap: 'nowrap' }}>
                    <span className="ico-chip"><IconZap /></span>
                    <div className="who">
                      <span className="mini-card__label">{t('keys.monthSpent')}</span>
                      <b className="mini-card__fig">{fmtSom(k.month_spent)}</b>
                      {k.monthly_limit != null && <span className="dim small">{t('keys.limitValue', { amount: fmtSom(k.monthly_limit) })}</span>}
                    </div>
                  </div>
                </div>
              )}
              <div className="mini-card mini-card--mint">
                <div className="hstack" style={{ gap: 12, flexWrap: 'nowrap' }}>
                  <span className="ico-chip"><IconClock /></span>
                  <div className="who">
                    <span className="mini-card__label">{t('keys.lastRequest')}</span>
                    <b className="mini-card__fig">{fmtAgo(k.last_used_at)}</b>
                    {k.last_used_at ? <span className="dim small">{fmtDateTime(k.last_used_at)}</span> : null}
                  </div>
                </div>
              </div>
            </div>

            <div className="key-grid key-grid--3">
              <div className="mini-card">
                <span className="mini-card__head"><span className="ico-chip ico-chip--sm"><IconShield /></span> {t('keys.permissions')}</span>
                <div className="hstack" style={{ gap: 7 }}>
                  <span className="tag">{t('keys.permChat')}</span><span className="tag">{t('keys.permModels')}</span><span className="tag">{t('keys.permStreaming')}</span><span className="tag">{t('keys.permFiles')}</span>
                </div>
              </div>
              <div className="mini-card">
                <span className="mini-card__head"><span className="ico-chip ico-chip--sm"><IconLayers /></span> {t('keys.usageLimit')}</span>
                {k.monthly_limit != null ? (
                  <div className="meter">
                    <b className="mini-card__fig">{t('keys.perMonth', { amount: fmtSom(k.monthly_limit) })}</b>
                    <span className="track"><span className="fill" style={{ width: `${used}%` }} /></span>
                    <span className="ends"><span>{fmtSom(k.month_spent)}</span><span>{used}%</span></span>
                  </div>
                ) : (
                  <>
                    <b className="mini-card__fig">{t('keys.unlimited')}</b>
                    <span className="dim small">{k.rpm_limit ? t('keys.rpm', { n: k.rpm_limit }) : t('keys.rpmDefault')}</span>
                  </>
                )}
              </div>
              <div className="mini-card">
                <span className="mini-card__head"><span className="ico-chip ico-chip--sm"><IconLink /></span> {t('keys.baseUrl')}</span>
                <div className="copy-row">
                  <div className="who"><b className="mono">{baseUrl}</b></div>
                  <CopyIcon text={baseUrl} title={t('keys.copyUrl')} />
                </div>
              </div>
            </div>
          </section>
        )
      })}

      {revoked.length > 0 && (
        <section className="panel block">
          <div className="panel-head">
            <div className="panel-head__t">
              <h2>{t('keys.revokedTitle')}</h2>
              <p>{t('keys.revokedLead')}</p>
            </div>
          </div>
          <div className="table-wrap"><table className="table">
            <thead><tr><th>{t('keys.name')}</th><th>{t('keys.colKey')}</th><th>{t('keys.colRevoked')}</th><th>{t('keys.colStatus')}</th></tr></thead>
            <tbody>
              {revoked.map((k) => (
                <tr key={k.id}>
                  <td><b>{k.name}</b></td>
                  <td><code className="dim">{k.prefix}...{k.last4}</code></td>
                  <td className="muted nowrap">{fmtAgo(k.revoked_at)}</td>
                  <td><span className="tag tag--bad"><i className="req-dot" />{t('keys.revokedTag')}</span></td>
                </tr>
              ))}
            </tbody>
          </table></div>
        </section>
      )}

      {creating && <CreateKey onClose={() => setCreating(false)} onDone={reload} />}
      {editing && <EditKey item={editing} onClose={() => setEditing(null)} onDone={reload} />}
      {revoking && (
        <Modal title={t('keys.revokeTitle')} onClose={() => setRevoking(null)} actions={<>
          <button className="btn" onClick={() => setRevoking(null)}>{t('common.no')}</button>
          <button className="btn btn--danger" onClick={revoke}>{t('keys.revokeConfirm')}</button>
        </>}>
          <p className="muted">{t('keys.revokeText', { name: revoking.name })}</p>
        </Modal>
      )}
    </>
  )
}
