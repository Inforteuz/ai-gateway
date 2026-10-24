// Minimal i18n: flat dictionaries per language, `{name}` interpolation and a
// fallback chain (current language -> Uzbek -> the key itself).
//
// Language priority: the viewer's explicit choice (localStorage) -> the
// server default (`site.default_lang`) -> "uz".
import { createContext, Fragment, isValidElement, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import uz from './uz.js'
import uzExtra from './uz-extra.js'

const DICTS = { uz: { ...uzExtra, ...uz } }
const pending = {}
function loadLanguage(lang) {
  if (DICTS[lang]) return Promise.resolve()
  if (!pending[lang]) {
    const modules = lang === 'ru' ? [import('./ru.js'), import('./ru-extra.js')] : [import('./en.js'), import('./en-extra.js')]
    pending[lang] = Promise.all(modules).then(([main, extra]) => {
      DICTS[lang] = { ...extra.default, ...main.default }
    }).catch((error) => { delete pending[lang]; throw error })
  }
  return pending[lang]
}
/** Languages offered in the switcher (an unknown `default_lang` falls back to Uzbek). */
export const LANGS = ['uz', 'ru', 'en']
export const FALLBACK_LANG = 'uz'
const STORAGE_KEY = 'sf-lang'

const isSupported = (l) => LANGS.includes(l)

function readStored() {
  try {
    const v = window.localStorage.getItem(STORAGE_KEY)
    return isSupported(v) ? v : null
  } catch {
    return null // private mode or blocked storage
  }
}

function writeStored(lang) {
  try { window.localStorage.setItem(STORAGE_KEY, lang) } catch { /* private mode */ }
}

// Mirrors the provider's language for code outside React (formatters, API errors).
let current = readStored() || FALLBACK_LANG
export const getLang = () => current

function lookup(lang, key) {
  const own = DICTS[lang]?.[key]
  if (own != null) return own
  const fb = DICTS[FALLBACK_LANG][key]
  return fb != null ? fb : key
}

/**
 * Translates `key` for `lang`. Plain values in `vars` are interpolated into
 * the string; if any value is a React element, an array of nodes is returned
 * instead so links and markup can be embedded in a sentence.
 */
export function translate(key, vars, lang = current) {
  const template = String(lookup(lang, key))
  if (!vars) return template
  const rich = Object.values(vars).some((v) => isValidElement(v))
  if (!rich) {
    return template.replace(/\{(\w+)\}/g, (m, name) => (name in vars && vars[name] != null ? String(vars[name]) : m))
  }
  return template.split(/(\{\w+\})/g).filter(Boolean).map((part, i) => {
    const m = /^\{(\w+)\}$/.exec(part)
    const value = m && m[1] in vars ? vars[m[1]] : part
    return <Fragment key={i}>{value}</Fragment>
  })
}

/** Locale tag for Intl/Date APIs. */
const LOCALES = { uz: 'uz-UZ', ru: 'ru-RU', en: 'en-US' }
export const localeOf = (lang = current) => LOCALES[lang] || LOCALES[FALLBACK_LANG]

const LangCtx = createContext({ lang: FALLBACK_LANG, setLang: () => {} })

export function LangProvider({ defaultLang, children }) {
  const [chosen, setChosen] = useState(readStored)
  const [, setRevision] = useState(0)
  const [loadError, setLoadError] = useState(false)
  const lang = chosen || (isSupported(defaultLang) ? defaultLang : FALLBACK_LANG)
  useEffect(() => {
    let alive = true
    setLoadError(false)
    loadLanguage(lang).then(() => { if (alive) setRevision(n => n + 1) }).catch(() => { if (alive) setLoadError(true) })
    return () => { alive = false }
  }, [lang])
  // Set during render so formatters called by children already see the new language.
  current = lang

  useEffect(() => { document.documentElement.lang = lang }, [lang])

  const setLang = useCallback((l) => {
    if (!isSupported(l)) return
    writeStored(l)
    setChosen(l)
  }, [])

  const value = useMemo(() => ({ lang, setLang }), [lang, setLang])
  if (!DICTS[lang]) return <div role="status" className="loading">{loadError
    ? <button className="btn" onClick={() => { setLoadError(false); loadLanguage(lang).then(() => setRevision(n => n + 1)).catch(() => setLoadError(true)) }}>↻ Retry / Повторить / Qayta urinish</button>
    : '…'}</div>
  return <LangCtx.Provider value={value}>{children}</LangCtx.Provider>
}

export const useLang = () => useContext(LangCtx)

/** Returns `t(key, vars)` bound to the current language. */
export function useT() {
  const { lang } = useContext(LangCtx)
  return useCallback((key, vars) => translate(key, vars, lang), [lang])
}

/** Translate static/dynamic interface labels; preserve numbers, markup and unknown data. */
export function translateText(value, vars) {
  if (Array.isArray(value)) return value.map((item) => translateText(item, vars))
  if (typeof value !== 'string') return value
  const key = value.replace(/\s+/g, ' ').trim()
  if (!Object.hasOwn(DICTS[FALLBACK_LANG], key)) return value
  return translate(key, vars)
}
