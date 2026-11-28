import { useEffect, useState } from 'react'
import { api, ApiError } from './api.js'

/**
 * Playground image results that outlive the page.
 *
 * The request runs here, not in the component: leaving the Playground while a picture is being
 * drawn no longer throws the result away (the user was already charged for it). Finished pictures
 * are kept per user in IndexedDB (base64 images do not fit in localStorage), the newest KEEP of them.
 * Without IndexedDB (private mode, blocked storage) the list still survives in-app navigation.
 */
const DB = 'sf-media'
const STORE = 'images'
const KEEP = 20

let dbPromise = null
function openDb() {
  if (dbPromise) return dbPromise
  dbPromise = new Promise((resolve) => {
    try {
      const req = indexedDB.open(DB, 1)
      req.onupgradeneeded = () => {
        const s = req.result.createObjectStore(STORE, { keyPath: 'id' })
        s.createIndex('user', 'userId')
      }
      req.onsuccess = () => resolve(req.result)
      req.onerror = () => resolve(null)
      req.onblocked = () => resolve(null)
    } catch { resolve(null) }
  })
  return dbPromise
}

async function dbPut(item) {
  const db = await openDb()
  if (!db) return
  try { db.transaction(STORE, 'readwrite').objectStore(STORE).put(item) } catch { /* storage full or blocked */ }
}

async function dbDelete(ids) {
  const db = await openDb()
  if (!db || !ids.length) return
  try {
    const s = db.transaction(STORE, 'readwrite').objectStore(STORE)
    ids.forEach((id) => s.delete(id))
  } catch { /* ignore */ }
}

async function dbLoad(userId) {
  const db = await openDb()
  if (!db) return []
  return new Promise((resolve) => {
    try {
      const req = db.transaction(STORE, 'readonly').objectStore(STORE).index('user').getAll(userId)
      req.onsuccess = () => resolve(req.result || [])
      req.onerror = () => resolve([])
    } catch { resolve([]) }
  })
}

// --- in-memory state shared by every mounted ImageStudio ----------------------------------------------------------
const state = { userId: null, items: [], loading: null }
const live = new Set()          // ids whose request runs in this page load
const listeners = new Set()
let chargedHook = null           // the mounted studio's balance refresh, if any

function emit() { listeners.forEach((fn) => fn(state.items)) }

function setItems(items) {
  state.items = items.sort((a, b) => b.started - a.started)
  const extra = state.items.slice(KEEP).map((i) => i.id)
  if (extra.length) { state.items = state.items.slice(0, KEEP); dbDelete(extra) }
  emit()
}

function patch(id, p) {
  let changed = null
  setItems(state.items.map((i) => (i.id === id ? (changed = { ...i, ...p }) : i)))
  if (changed && changed.status !== 'running') dbPut(changed)
}

async function ensureLoaded(userId) {
  const uid = String(userId || 'anon')
  if (state.userId === uid) return state.loading
  state.userId = uid
  state.items = []
  emit()
  state.loading = dbLoad(uid).then((saved) => {
    if (state.userId !== uid) return
    // A picture that was still drawing when the page was reloaded cannot be picked up again
    const restored = saved.map((i) => (i.status === 'running' && !live.has(i.id)
      ? { ...i, status: 'failed', interrupted: true, error: null }
      : i))
    restored.filter((i, k) => i.interrupted && saved[k].status === 'running').forEach(dbPut)
    const mine = state.items.filter((i) => live.has(i.id))
    setItems([...mine, ...restored.filter((i) => !mine.some((m) => m.id === i.id))])
  })
  return state.loading
}

export function useImageItems(userId, onCharged) {
  const [items, setLocal] = useState(state.items)
  useEffect(() => {
    listeners.add(setLocal)
    ensureLoaded(userId)
    setLocal(state.items)
    return () => { listeners.delete(setLocal) }
  }, [userId])
  useEffect(() => {
    chargedHook = onCharged || null
    return () => { if (chargedHook === onCharged) chargedHook = null }
  }, [onCharged])
  return items
}

export async function startImage({ userId, body, modelName, edited }) {
  await ensureLoaded(userId)
  const id = crypto.randomUUID?.() || String(Date.now())
  const item = { id, userId: state.userId, prompt: body.prompt, model: modelName, status: 'running',
    started: Date.now(), edited: Boolean(edited) }
  live.add(id)
  setItems([item, ...state.items])
  dbPut(item)                      // so a reload shows what happened to it
  try {
    const res = await fetch('/portal/playground/images', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-SF-CSRF': '1' },
      body: JSON.stringify(body),
    })
    const data = await res.json().catch(() => null)
    if (!res.ok) {
      const err = new ApiError(res.status, data?.error?.code || 'error', data?.error?.message || `HTTP ${res.status}`)
      patch(id, { status: 'failed', error: err.message, code: res.status })
      return
    }
    const img = data?.data?.[0]
    patch(id, { status: 'done', src: img ? `data:${img.mime_type || 'image/png'};base64,${img.b64_json}` : null })
    const rid = res.headers.get('X-Request-Id')
    if (rid) {
      for (let k = 0; k < 6; k++) {
        const r = await api.get(`/portal/playground/receipt/${rid}`).catch(() => null)
        if (r?.ready) { patch(id, { cost: r.cost }); break }
        await new Promise((ok) => setTimeout(ok, 500 + k * 300))
      }
    }
    chargedHook?.()
  } catch {
    patch(id, { status: 'failed', error: null, network: true })
  } finally {
    live.delete(id)
  }
}

export function removeImage(id) {
  setItems(state.items.filter((i) => i.id !== id))
  dbDelete([id])
}
