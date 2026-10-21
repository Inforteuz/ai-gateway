import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../../api.js'
import { useSite } from '../../auth.js'

// Screen of a single instance (noVNC). The connection goes through the backend: a one-time
// ticket + the admin session; the backend supplies the VNC password, it never reaches the browser.
// All Chromes share one virtual display — when a ticket is issued, this instance's
// window is brought to the front.

const STATUS = {
  ticket: 'Ruxsat olinmoqda…',
  connecting: 'Ulanmoqda…',
  connected: 'Ulangan',
  closed: 'Ulanish yopildi',
}

export default function Vnc() {
  const { n } = useParams()
  const site = useSite()
  const box = useRef(null)
  const rfb = useRef(null)
  // Sequence number of each connection attempt: if the page closes or reconnect is clicked
  // while a ticket is pending, a stale attempt must not create an RFB (the session would stay open).
  const attempt = useRef(0)
  const [status, setStatus] = useState('ticket')
  const [error, setError] = useState('')

  const disconnect = useCallback(() => {
    attempt.current += 1
    try { rfb.current?.disconnect() } catch { /* already closed */ }
    rfb.current = null
  }, [])

  const connect = useCallback(async () => {
    disconnect()
    const my = attempt.current
    setError('')
    setStatus('ticket')
    try {
      const [{ ticket, path }, { default: RFB }] = await Promise.all([
        api.post(`/portal/admin/instances/${n}/vnc-ticket`),
        import('@novnc/novnc/core/rfb.js'),
      ])
      if (my !== attempt.current) return
      setStatus('connecting')
      const proto = window.location.protocol === 'https:' ? 'wss' : 'ws'
      const url = `${proto}://${window.location.host}${path}?ticket=${encodeURIComponent(ticket)}`
      const r = new RFB(box.current, url, { wsProtocols: ['binary'] })
      r.scaleViewport = true
      r.resizeSession = false
      r.addEventListener('connect', () => setStatus('connected'))
      r.addEventListener('disconnect', (e) => {
        if (my !== attempt.current) return   // deliberately disconnected or superseded by a newer attempt
        setStatus('closed')
        if (!e.detail?.clean) setError('Ulanish uzildi. Qayta ulaning.')
      })
      rfb.current = r
    } catch (e) {
      if (my !== attempt.current) return
      setStatus('closed')
      setError(e.message || 'Ulanib bo\'lmadi')
    }
  }, [n, disconnect])

  useEffect(() => {
    const prev = document.title
    document.title = `VNC #${n} — ${site.brand_name}`
    return () => { document.title = prev }
  }, [n, site.brand_name])

  useEffect(() => {
    connect()
    return disconnect
  }, [connect, disconnect])

  return (
    <div style={{ position: 'fixed', inset: 0, display: 'flex', flexDirection: 'column', background: '#111', zIndex: 50 }}>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center', padding: '8px 14px', background: 'var(--surface, #1b1b1b)', color: 'var(--text, #eee)', flexWrap: 'wrap' }}>
        <b>Instansiya #{n}</b>
        <span className="muted small">{STATUS[status]}</span>
        {error && <span className="small" style={{ color: 'var(--bad, #f66)' }}>{error}</span>}
        <span style={{ marginLeft: 'auto', display: 'flex', gap: 8 }}>
          <button type="button" className="btn btn--small" onClick={connect}>Qayta ulanish</button>
          <button type="button" className="btn btn--small" onClick={() => { disconnect(); setStatus('closed') }}>Uzish</button>
          <Link className="btn btn--small" to="/app/admin/instances">Instansiyalar</Link>
        </span>
      </div>
      <div ref={box} style={{ flex: 1, minHeight: 0, overflow: 'hidden' }} />
    </div>
  )
}
