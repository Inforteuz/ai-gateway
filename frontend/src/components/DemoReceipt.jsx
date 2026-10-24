import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { useEffect, useMemo, useState } from 'react'
import { useSite } from '../auth.js'
import { MICRO, fmtInt, fmtNumber, fmtSom } from '../format.js'
import { MODEL_PLACEHOLDER } from '../site.js'
import { useT } from '../i18n/index.jsx'

// Prompt texts live in the dictionaries under `receipt.sample.<id>`.
const SAMPLES = [
  { tin: 12, tout: 38, id: 'a91f3c' },
  { tin: 21, tout: 17, id: '3be07d' },
  { tin: 14, tout: 186, id: 'c5d2e8' },
]
const START_BALANCE = 50_000 * MICRO
const cost = (tokens, per1m) => Math.ceil((tokens * per1m) / MICRO)

/** Live receipt on the landing page: the prompt is typed, tokens are counted, the price appears. Cycles quietly. */
export default function DemoReceipt({ priceIn, priceOut, model = MODEL_PLACEHOLDER, free = false }) {
  useLocaleRefresh()
  const site = useSite()
  const t = useT()
  const reduce = useMemo(() => typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches, [])
  const [n, setN] = useState(0)           // which sample
  const [typed, setTyped] = useState(reduce ? Infinity : 0)
  const [step, setStep] = useState(reduce ? 5 : 0) // 0 typing, 1 input, 2 output, 3 total, 4 balance, 5 done
  const [count, setCount] = useState(reduce ? SAMPLES[0].tout : 0)
  const s = SAMPLES[n]
  const prompt = t(`receipt.sample.${s.id}`)

  useEffect(() => {
    if (reduce) return undefined
    setTyped(0); setStep(0); setCount(0)
    const timers = []
    const at = (ms, fn) => timers.push(setTimeout(fn, ms))
    let ms = 350
    for (let i = 1; i <= prompt.length; i++) { const k = i; at(ms + i * 34, () => setTyped(k)) }
    ms += prompt.length * 34 + 450
    at(ms, () => setStep(1))
    at(ms + 450, () => setStep(2))
    const ticks = 22
    for (let i = 1; i <= ticks; i++) { const k = i; at(ms + 500 + i * 38, () => setCount(Math.round((s.tout * k) / ticks))) }
    at(ms + 500 + ticks * 38 + 250, () => setStep(3))
    at(ms + 500 + ticks * 38 + 750, () => setStep(4))
    at(ms + 500 + ticks * 38 + 1100, () => setStep(5))
    at(ms + 500 + ticks * 38 + 4600, () => setN((x) => (x + 1) % SAMPLES.length))
    return () => timers.forEach(clearTimeout)
  }, [n, reduce, prompt, s.tout])

  const cin = free ? 0 : cost(s.tin, priceIn), cout = free ? 0 : cost(s.tout, priceOut), total = cin + cout
  return (
    <div className="receipt" role="img" aria-label={t('receipt.aria', { in: s.tin, out: s.tout, total: free ? t('receipt.free') : t('receipt.totalAmount', { amount: fmtSom(total) }) })}>
      <div className="receipt-head"><b>{tr(site.brand_name)}</b><span>{t('receipt.title')}</span></div>
      <div className="rline"><span>{t('receipt.model')}</span><span>{tr(model)}</span></div>
      <div className="rline"><span>{t('receipt.prompt')}</span><span /></div>
      <div className="receipt-prompt">
        {prompt.slice(0, typed)}{step === 0 && <span className="caret" />}
      </div>
      {step >= 1 && (
        <div className="rline rline--item print-in">
          <div className="rline"><span>{t('receipt.inTokens')}</span><span>{fmtInt(s.tin)}</span></div>
          {!free && <div className="rline rcalc"><span>{fmtInt(s.tin)} x {fmtNumber(priceIn / MICRO)} {t('common.som')} / 1M</span><span>{fmtSom(cin)}</span></div>}
        </div>
      )}
      {step >= 2 && (
        <div className="rline rline--item print-in">
          <div className="rline"><span>{t('receipt.outTokens')}</span><span>{fmtInt(count)}</span></div>
          {!free && <div className="rline rcalc"><span>{fmtInt(s.tout)} x {fmtNumber(priceOut / MICRO)} {t('common.som')} / 1M</span><span>{tr(step >= 3 ? fmtSom(cout) : '...')}</span></div>}
        </div>
      )}
      {step >= 3 && (<><hr className="rsep" /><div className="rtotal print-in"><span>{tr(free ? t('receipt.payment') : t('receipt.total'))}</span><b>{tr(free ? t('receipt.free') : fmtSom(total))}</b></div></>)}
      {step >= 4 && !free && <div className="rline print-in" style={{ marginTop: 6 }}><span>{t('receipt.left')}</span><span>{fmtSom(START_BALANCE - total)}</span></div>}
      {step >= 5 && <div className="rfoot print-in">{t('receipt.foot')} req_{tr(s.id)}</div>}
    </div>
  )
}
