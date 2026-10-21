// Reasoning levels: per-key setting and the global default.
import { useT } from './i18n/index.jsx'

// Per-key levels; labels live in the dictionaries under `level.key.*`.
export const KEY_LEVELS = ['inherit', 'none', 'low', 'medium', 'high', 'model']

// Admin-only (global default); not translated yet.
export const DEFAULT_LEVELS = [
  ['none', 'O\'chiq: tez va arzon, murakkab savollarda xato qilishi mumkin'],
  ['low', 'Past (low): tezlik va sifat o\'rtasida muvozanat (tavsiya etiladi)'],
  ['medium', 'O\'rta (medium)'],
  ['high', 'Yuqori (high): eng chuqur, lekin sekin va qimmat'],
  ['', 'Modelning o\'zi hal qiladi'],
]

// Uzbek short names, kept for pages that are not translated yet (admin tape).
export const LEVEL_SHORT = { none: 'o\'chiq', low: 'past', medium: 'o\'rta', high: 'yuqori', model: 'modelniki' }

/** Translated short name of a level (falls back to the raw value). */
export function levelShort(t, level) {
  return LEVEL_SHORT[level] ? t(`level.short.${level}`) : level
}

export function LevelSelect({ value, onChange, id }) {
  const t = useT()
  return (
    <select id={id} className="input" value={value} onChange={(e) => onChange(e.target.value)}>
      {KEY_LEVELS.map((v) => <option key={v} value={v}>{t(`level.key.${v}`)}</option>)}
    </select>
  )
}
