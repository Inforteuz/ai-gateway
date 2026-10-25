import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

// Every `var(--token)` must name a token the design system defines.
//
// An undefined token fails silently, two ways. With a fallback —
// `var(--accent-soft, #eaf8f2)` — the browser uses that literal in BOTH themes, so a
// light mint selection background showed up in dark mode with white text on it,
// unreadable (2026-10-25). Without a fallback the property simply does not apply.
// Eight such names had crept in (`--accent`, `--muted`, `--surface`, `--wash` ...),
// none defined anywhere.
//
// Use the real tokens: they are redefined for dark mode, which is the whole point.

const src = path.resolve(path.dirname(new URL(import.meta.url).pathname), '../src')

test('every CSS custom property in use is defined', () => {
  const css = fs.readFileSync(path.join(src, 'styles.css'), 'utf8')
  const defined = new Set([...css.matchAll(/^\s*(--[a-z0-9-]+)\s*:/gm)].map(m => m[1]))

  const files = [path.join(src, 'styles.css')]
  const walk = d => { for (const f of fs.readdirSync(d, { withFileTypes: true })) {
    const p = path.join(d, f.name)
    if (f.isDirectory()) walk(p); else if (/\.jsx?$/.test(f.name)) files.push(p) } }
  walk(src)

  const undefinedUses = []
  for (const file of files) {
    for (const m of fs.readFileSync(file, 'utf8').matchAll(/var\((--[a-zA-Z0-9-]+)/g)) {
      // `var(--viz-${i})` is built at runtime; `--viz-1` … are defined
      if (m[1] === '--viz-') continue
      if (!defined.has(m[1])) undefinedUses.push(`${path.relative(src, file)}: ${m[1]}`)
    }
  }
  assert.deepEqual([...new Set(undefinedUses)], [])
})
