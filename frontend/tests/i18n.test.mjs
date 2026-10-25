import test from 'node:test'
import assert from 'node:assert/strict'
import uz from '../src/i18n/uz.js'
import en from '../src/i18n/en.js'
import ru from '../src/i18n/ru.js'
import uzExtra from '../src/i18n/uz-extra.js'
import enExtra from '../src/i18n/en-extra.js'
import ruExtra from '../src/i18n/ru-extra.js'
const placeholders = s => [...String(s).matchAll(/\{(\w+)\}/g)].map(m => m[1]).sort()
for (const [name, dicts] of [['main', { uz, en, ru }], ['extended', { uz: uzExtra, en: enExtra, ru: ruExtra }]]) {
  test(`${name}: all three languages have complete keys and placeholders`, () => {
    const keys = Object.keys(dicts.uz).sort()
    for (const lang of ['en', 'ru']) {
      assert.deepEqual(Object.keys(dicts[lang]).sort(), keys)
      for (const key of keys) {
        assert.equal(typeof dicts[lang][key], 'string', `${lang}: ${key}`)
        assert.ok(dicts[lang][key].trim(), `${lang}: empty ${key}`)
        assert.deepEqual(placeholders(dicts[lang][key]), placeholders(dicts.uz[key]), `${lang}: ${key}`)
      }
    }
  })
}

// --- Text handed to tr() must exist in the dictionary ---------------------------
//
// translateText() looks the Uzbek text up as a KEY and, when it is missing, returns
// it unchanged — no error, no warning. So a string wrapped in tr() but never added
// to the dictionary stays Uzbek in the Russian and English interface, and nothing
// says so. That is how "Tekshirish", "joyida" and a dozen others reached production
// (2026-10-25): every one of them was correctly wrapped, none was translatable.
//
// This scans the WHOLE argument of each tr(...) call — including both arms of a
// conditional, which is where most of them hid — and fails on any natural-language
// literal that has no entry.
import fs from 'node:fs'
import path from 'node:path'

// Literals that appear inside a tr(...) call but are never shown: values a
// condition compares against, and CSS class names. Add here only what is not text.
const NOT_TEXT = new Set([
  'active', 'cleanup', 'cluster', 'cost', 'dim small', 'down', 'external', 'hbar',
  'inherit', 'inline-block', 'inst-mem__bar', 'login', 'muted', 'num', 'pending',
  'probe', 'quota', 'recover-memory', 'register', 'reload', 'sending', 'serif',
  'tag tag--bad', 'tokens', 'track',
])

function trArguments(src) {
  const out = []
  for (const m of src.matchAll(/\btr\(/g)) {
    let depth = 0, quote = null
    for (let i = m.index + 2; i < src.length; i++) {
      const c = src[i]
      if (quote) { if (c === '\\') { i++; continue } if (c === quote) quote = null; continue }
      if (c === '"' || c === "'" || c === '`') { quote = c; continue }
      if (c === '(') depth++
      else if (c === ')' && --depth === 0) { out.push(src.slice(m.index + 2, i + 1)); break }
    }
  }
  return out
}

test('every literal handed to tr() has a dictionary entry', () => {
  const keys = new Set(Object.keys({ ...uz, ...uzExtra }).map(k => k.replace(/\s+/g, ' ').trim()))
  const root = path.resolve(path.dirname(new URL(import.meta.url).pathname), '../src')
  const files = []
  const walk = d => { for (const f of fs.readdirSync(d, { withFileTypes: true })) {
    const p = path.join(d, f.name)
    if (f.isDirectory()) { if (f.name !== 'i18n') walk(p) } else if (/\.jsx?$/.test(f.name)) files.push(p) } }
  walk(root)

  const missing = []
  for (const file of files) {
    for (const arg of trArguments(fs.readFileSync(file, 'utf8'))) {
      for (const lit of arg.matchAll(/(["'`])((?:\\.|(?!\1).)*?)\1/g)) {
        const s = lit[2].replace(/\\(["'`])/g, '$1').replace(/\s+/g, ' ').trim()
        if (!s || s.includes('${') || !/[A-Za-z]{3}/.test(s)) continue
        if (keys.has(s) || NOT_TEXT.has(s)) continue
        missing.push(`${path.relative(root, file)}: ${JSON.stringify(s)}`)
      }
    }
  }
  assert.deepEqual([...new Set(missing)], [],
    'wrapped in tr() but missing from src/i18n/*-extra.js — it would stay Uzbek in ru/en')
})
