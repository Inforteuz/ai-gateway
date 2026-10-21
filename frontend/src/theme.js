// Color theme: auto (system), light or dark.
// Persisted in localStorage and applied as <html data-theme="...">.
import { useEffect, useState } from 'react'

const KEY = 'sf-theme'
const VALUES = ['auto', 'light', 'dark']

function apply(mode) {
  const html = document.documentElement
  if (mode === 'auto') html.removeAttribute('data-theme')
  else html.setAttribute('data-theme', mode)
}

function readStored() {
  try {
    const stored = localStorage.getItem(KEY)
    return VALUES.includes(stored) ? stored : 'auto'
  } catch {
    return 'auto' // private mode or blocked storage
  }
}

export function initTheme() {
  apply(readStored())
}

export function useTheme() {
  const [mode, setMode] = useState(readStored)
  useEffect(() => {
    apply(mode)
    try { localStorage.setItem(KEY, mode) } catch { /* private window */ }
  }, [mode])
  // Cycles through the modes: auto → light → dark → auto
  const cycle = () => setMode((m) => VALUES[(VALUES.indexOf(m) + 1) % VALUES.length])
  return { mode, setMode, cycle }
}
