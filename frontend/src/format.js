// Money is stored on the server as integer "micro-som" (1 som = 1 000 000).
// Formatters read the current UI language, so callers need no extra argument.
import { getLang, translate, localeOf } from './i18n/index.jsx'

export const MICRO = 1_000_000

const nbsp = ' '

function group(intStr) {
  return intStr.replace(/\B(?=(\d{3})+(?!\d))/g, nbsp)
}

/** 12345.5 -> "12 345,5" (space for thousands, comma for decimals — same in uz and ru). */
export function fmtNumber(value, decimals = 0) {
  return new Intl.NumberFormat(localeOf(), { minimumFractionDigits: decimals, maximumFractionDigits: decimals }).format(Number(value) || 0)
}

/** Som amount in a readable form: large sums rounded, small ones precise. */
export function fmtSom(micro, { sign = false, unit = true } = {}) {
  const v = (micro || 0) / MICRO
  const abs = Math.abs(v)
  let s
  if (abs >= 1000) s = fmtNumber(v, 0)
  else if (abs >= 1) s = fmtNumber(v, 2)
  else if (abs === 0) s = '0'
  else s = fmtNumber(v, abs < 0.01 ? 6 : 4).replace(/[,.]?0+$/, '')
  if (sign && v > 0) s = '+' + s
  return unit ? `${s}${nbsp}${translate('common.som')}` : s
}

export const fmtInt = (n) => fmtNumber(n || 0, 0)

const MONTHS = {
  en: ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'],
  uz: ['yan', 'fev', 'mar', 'apr', 'may', 'iyn', 'iyl', 'avg', 'sen', 'okt', 'noy', 'dek'],
  ru: ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'],
}
const MONTHS_LONG = {
  en: ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'],
  uz: ['yanvar', 'fevral', 'mart', 'aprel', 'may', 'iyun', 'iyul', 'avgust', 'sentabr', 'oktabr', 'noyabr', 'dekabr'],
  ru: ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября', 'октября', 'ноября', 'декабря'],
}
const monthsShort = () => MONTHS[getLang()] || MONTHS.uz
const monthsLong = () => MONTHS_LONG[getLang()] || MONTHS_LONG.uz
// Uzbek writes "20-oktabr", Russian "20 октября"
const dayMonth = (day, month) => (getLang() === 'uz' ? `${day}-${month}` : `${day} ${month}`)

const tz = { timeZone: 'Asia/Tashkent' }

function parts(ts) {
  const f = new Intl.DateTimeFormat('en-GB', { ...tz, day: 'numeric', month: 'numeric', year: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })
  const o = {}
  for (const p of f.formatToParts(new Date(ts * 1000))) o[p.type] = p.value
  return o
}

export function fmtDateTime(ts) {
  if (!ts) return '-'
  const p = parts(ts)
  return `${dayMonth(p.day, monthsShort()[Number(p.month) - 1])}, ${p.hour}:${p.minute}`
}

export function fmtTime(ts) {
  if (!ts) return '-'
  const p = parts(ts)
  return `${p.hour}:${p.minute}:${p.second}`
}

/** Today's date: "20-oktabr, 2026" / "20 октября, 2026". */
export function fmtDateLong(date = new Date()) {
  const f = new Intl.DateTimeFormat('en-GB', { ...tz, day: 'numeric', month: 'numeric', year: 'numeric' })
  const o = {}
  for (const p of f.formatToParts(date)) o[p.type] = p.value
  return `${dayMonth(Number(o.day), monthsLong()[Number(o.month) - 1])}, ${o.year}`
}

export function fmtDay(ymd) {
  const [, m, d] = ymd.split('-')
  return dayMonth(Number(d), monthsShort()[Number(m) - 1])
}

export function fmtAgo(ts) {
  if (!ts) return translate('time.never')
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts))
  if (s < 60) return translate('time.justNow')
  if (s < 3600) return translate('time.minutesAgo', { n: Math.floor(s / 60) })
  if (s < 86400) return translate('time.hoursAgo', { n: Math.floor(s / 3600) })
  if (s < 86400 * 30) return translate('time.daysAgo', { n: Math.floor(s / 86400) })
  return fmtDateTime(ts)
}

export const somToMicro = (som) => Math.round(Number(som) * MICRO)
export const microToSom = (micro) => micro / MICRO

/** 3725 -> "1 soat 2 daqiqa" / "1 ч 2 мин" */
export function fmtDuration(seconds) {
  const s = Math.max(0, Math.floor(seconds || 0))
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), r = s % 60
  const out = []
  if (d) out.push(translate('time.days', { n: d }))
  if (h) out.push(translate('time.hours', { n: h }))
  if (m && !d) out.push(translate('time.minutes', { n: m }))
  if (r && !d && !h) out.push(translate('time.seconds', { n: r }))
  return out.join(' ') || translate('time.seconds', { n: 0 })
}

export const fmtPct = (v) => (v == null ? translate('common.noData') : `${fmtNumber(v, v >= 99.995 || v === 0 ? 0 : 2)}%`)
