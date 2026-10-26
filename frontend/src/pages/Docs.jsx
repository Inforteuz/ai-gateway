import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { useEffect, useState } from 'react'
import { api } from '../api.js'
import { useAuth, useSite } from '../auth.js'
import { META_FIELD, apiOrigin, pickModel, reasoningModels } from '../site.js'
import { CodeBlock, CodeTabs, CopyIcon, useLoad } from '../components/ui.jsx'
import {
  IconArrowRight, IconBook, IconBulb, IconCode, IconDoc, IconHeadset,
  IconLayers, IconLink, IconShield, IconZap,
} from '../components/icons.jsx'
import { MICRO, fmtNumber } from '../format.js'
import { snippets } from './snippets.js'

const ERRORS = [
  ['400', 'invalid_json, invalid_messages, invalid_max_tokens, upstream_rejected', 'So\'rov noto\'g\'ri tuzilgan yoki model uni qabul qilmadi (masalan, kontekst juda uzun). Pul yechilmaydi.'],
  ['401', 'invalid_api_key, revoked_api_key', 'Kalit berilmagan, noto\'g\'ri yoki bekor qilingan.'],
  ['402', 'insufficient_balance', 'Hisobda mablag\' yetarli emas. Hisobni to\'ldiring yoki max_tokens ni kamaytiring.'],
  ['403', 'account_suspended', 'Hisob to\'xtatilgan.'],
  ['404', 'model_not_found', 'Bunday model yo\'q yoki o\'chirilgan. Javobda mavjud modellar ro\'yxati bor.'],
  ['413', 'request_too_large', 'So\'rov hajmi 2 MB dan oshdi.'],
  ['429', 'rate_limit_exceeded, too_many_concurrent, key_limit_reached', 'Chegara oshdi. Retry-After sarlavhasida kutish vaqti (soniya) bor.'],
  ['502/503/504', 'upstream_error, model_offline, server_busy, upstream_timeout', 'Model serveri band yoki ulanmagan. Pul yechilmaydi, birozdan keyin qayta urinib ko\'ring.'],
]

// Labels for the capabilities column in the models table
const DOC_CAPS = [
  ['vision', 'rasm'],
  ['file_input', 'fayl'],
  ['image_generation', 'rasm yaratish'],
  ['tool_calling', 'agent'],
]

const SECTIONS = [
  ['boshlash', 'Boshlash', <IconZap key="i" />],
  ['autentifikatsiya', 'Autentifikatsiya', <IconShield key="i" />],
  ['chat', 'Chat so\'rovi', <IconCode key="i" />],
  ['modellar', 'Modellar', <IconLayers key="i" />],
  ['hisob', 'Modellar va hisob', <IconDoc key="i" />],
  ['narx', 'Narx', <IconBulb key="i" />],
  ['chegaralar', 'Chegaralar', <IconLink key="i" />],
  ['xatolar', 'Xatolar', <IconBook key="i" />],
]

/** Detects which section is visible while scrolling (to highlight the side menu). */
function useActiveSection(ids) {
  const [active, setActive] = useState(ids[0])
  useEffect(() => {
    const pick = () => {
      let cur = ids[0]
      for (const id of ids) {
        const el = document.getElementById(id)
        if (el && el.getBoundingClientRect().top <= 140) cur = id
      }
      setActive(cur)
    }
    pick()
    window.addEventListener('scroll', pick, { passive: true })
    window.addEventListener('resize', pick)
    return () => { window.removeEventListener('scroll', pick); window.removeEventListener('resize', pick) }
  }, [ids])
  return active
}

const SECTION_IDS = SECTIONS.map(([id]) => id)

/** Renders model ids as comma-separated <code> elements. */
const codeList = (ms) => ms.map((x, i) => <span key={x.id}>{tr(i ? ', ' : '')}<code>{x.id}</code></span>)

export default function Docs({ inApp }) {
  useLocaleRefresh()
  const active = useActiveSection(SECTION_IDS)
  const { freeMode } = useAuth()
  const { data } = useLoad(() => api.get('/portal/models'), [])
  const site = useSite()
  const items = data?.items || []
  const m = items[0]
  // Model names in examples come from the catalog: the first model with the required capability
  const model = pickModel(items)
  const thinking = reasoningModels(items)
  const thinkModel = thinking[0]?.id || model
  const visionModel = pickModel(items, 'vision')
  const fileModel = pickModel(items, 'file_input')
  const imageModels = items.filter((x) => x.capabilities?.image_generation)
  const imageModel = pickModel(items, 'image_generation')
  const editModels = items.filter((x) => x.capabilities?.image_editing)
  const editModel = pickModel(items, 'image_editing')
  const toolModel = pickModel(items, 'tool_calling')
  const limits = data?.limits
  const base = apiOrigin(site)
  const content = (
    <div className="prose">
      <nav className="crumbs" aria-label={tr("Yo'l")}><span>{tr("Hujjatlar")}</span><i>/</i><span>{tr("Boshlash")}</span></nav>
      <h1 className="docs-title">{tr(site.brand_name)} <em>{tr("hujjatlari")}</em></h1>
      <p>{tr(site.brand_name)} {tr("OpenAI formatiga mos. Mavjud kutubxonangizda faqat")} <code>base_url</code> {tr("va")} <code>api_key</code> {tr("ni almashtirsangiz yetarli.")}</p>
      <div className="docs-highlights">
        <div className="mini-card"><span className="mini-card__head"><span className="ico-chip ico-chip--sm"><IconZap /></span> {tr("OpenAI bilan 1:1 mos")}</span><span className="dim small">{tr("Mavjud SDK, prompt va kodlaringiz ishlaydi")}</span></div>
        <div className="mini-card"><span className="mini-card__head"><span className="ico-chip ico-chip--sm"><IconLayers /></span> {tr("Bir nechta model")}</span><span className="dim small">{tr(site.assistant_name)} {tr("modellari va kelajakda yangi imkoniyatlar")}</span></div>
        <div className="mini-card"><span className="mini-card__head"><span className="ico-chip ico-chip--sm"><IconShield /></span> {tr("Tez va ishonchli")}</span><span className="dim small">{tr("Barqaror infratuzilma va yuqori ishlash")}</span></div>
        <div className="mini-card"><span className="mini-card__head"><span className="ico-chip ico-chip--sm"><IconCode /></span> {tr("Oddiy integratsiya")}</span><span className="dim small">{tr("Bir necha daqiqada ishga tushiring")}</span></div>
      </div>

      <h2 id="boshlash">{tr("Boshlash")}</h2>
      <p>{tr("Panelning \"API kalitlar\" bo'limida kalit yarating, so'ng so'rov yuboring:")}</p>
      <CodeTabs tabs={snippets(base, 'sk-sf-...', model)} />

      <h2 id="autentifikatsiya">{tr("Autentifikatsiya")}</h2>
      <p>{tr("Har bir so'rovda kalit")} <code>Authorization</code> {tr("sarlavhasida yuboriladi. Kalitni brauzer kodiga yoki ommaviy repozitoriyga qo'ymang: u hisobingizdan pul sarflaydi.")}</p>
      <CodeBlock code={'Authorization: Bearer sk-sf-...'} />

      <h2 id="chat">{tr("Chat so'rovi")}</h2>
      <div className="endpoint"><span className="verb">POST</span><code>/v1/chat/completions</code></div>
      <ul>
        <li><code>model</code> {tr("(majburiy):")} {tr(model)}</li>
        <li><code>messages</code> {tr("(majburiy):")} <code>role</code> {tr("va")} <code>content</code> {tr("li ro'yxat")}</li>
        <li><code>max_tokens</code>{tr(": javobdagi tokenlarning yuqori chegarasi. Berilmasa")} {fmtNumber(limits?.default_max_tokens ?? 4096)}{tr(", eng ko'pi")} {fmtNumber(limits?.max_max_tokens ?? 16384)}</li>
        <li><code>stream</code>: <code>true</code> {tr("bo'lsa javob oqim bilan keladi (SSE)")}</li>
        <li><code>temperature</code>, <code>top_p</code>, <code>stop</code>, <code>tools</code> {tr("va boshqa standart parametrlar model tomoniga uzatiladi")}</li>
      </ul>
      <p>{tr("Matn davom ettirish uchun")} <code>POST /v1/completions</code> {tr("ham bor (")}<code>prompt</code> {tr("bilan).")}</p>
      <h3>{tr("Fikrlash (o'ylash) rejimi")}</h3>
      <p>{tr(thinking.length ? <>{tr("O'ylash")} {codeList(thinking)} {tr(thinking.length > 1 ? 'modellarida' : 'modelida')} {tr("yoqilgan")}</> : tr("O'ylash faqat ayrim modellarda yoqilgan"))}{tr(": ular javob berishdan oldin o'ylaydi, shuning uchun murakkab hisob-kitob, mantiq va kodda aniqroq, lekin sezilarli sekinroq. Qolgan modellarda")} <code>reasoning_effort</code> {tr("yuborsangiz ham e'tiborsiz qoladi. Qaysi modelda qanday darajalar borligini")} <code>GET /v1/models</code> {tr("javobidagi")} <code>capabilities.reasoning_levels</code> {tr("ko'rsatadi.")}</p>
      <CodeBlock code={`resp = client.chat.completions.create(
    model="${thinkModel}",
    messages=[{"role": "user", "content": "Murakkab masalani bosqichma-bosqich yech: ..."}],
    extra_body={"reasoning_effort": "low"},  # none | low | medium | high (berilmasa medium)
)`} />
      <h3>{tr("Oqim (stream)")}</h3>
      <CodeBlock code={`stream = client.chat.completions.create(model="${model}", stream=True,
    messages=[{"role": "user", "content": "Salom!"}])
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")`} />
      <p>{tr("Oqimda ham hisob token soniga qarab yuritiladi. Mijoz oqimni o'rtasida uzsa, shu paytgacha yaratilgan tokenlar uchun to'lanadi.")}</p>

      <h3>{tr("Rasm va fayl yuborish")}</h3>
      <p>{tr("Rasm yoki hujjatni OpenAI formatida")} <code>content</code> {tr("massivi orqali yuboriladi. Qaysi model nimani qabul qilishini quyidagi jadvaldagi")} <b>{tr("Imkoniyatlar")}</b> {tr("ustuni ko'rsatadi — imkoniyati yo'q modelga yuborilsa, so'rov xato bilan qaytadi (jim e'tiborsiz qoldirilmaydi).")}</p>
      <CodeBlock code={`# Rasmni tahlil qilish
resp = client.chat.completions.create(
    model="${visionModel}",
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "Bu rasmda nima bor?"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}},
    ]}],
)

# Hujjatni tahlil qilish
resp = client.chat.completions.create(
    model="${fileModel}",
    messages=[{"role": "user", "content": [
        {"type": "text", "text": "Bu hisobotda eng yuqori oy qaysi?"},
        {"type": "file", "file": {"filename": "hisobot.pdf",
                                  "file_data": "data:application/pdf;base64,..."}},
    ]}],
)`} />
      <ul>
        <li>{tr("Rasm")} <code>data:</code> {tr("ko'rinishida yoki ochiq")} <code>https://</code> {tr("havola bo'lishi mumkin. Ichki tarmoq manzillari qabul qilinmaydi.")}</li>
        <li>{tr("Bitta fayl 12 MB gacha, bitta so'rovda jami 30 MB va ko'pi bilan 6 ta fayl.")}</li>
        <li>{tr("Qabul qilinadigan turlar: png, jpeg, webp, gif, bmp, pdf, txt, csv, md, json, doc(x), xls(x).")}</li>
        <li>{tr("Fayl biriktirilgan so'rov uzoqroq davom etadi, shuning uchun mijoz tomonidagi timeout'ni kamida 120 soniya qilib qo'ying.")}</li>
      </ul>

      <h3>{tr("Rasm yaratish")}</h3>
      <div className="endpoint"><span className="verb">POST</span><code>/v1/images/generations</code></div>
      <p>{tr("Natija har doim")} <code>b64_json</code> {tr("ko'rinishida qaytadi (havola emas), shuning uchun uni to'g'ridan-to'g'ri saqlash yoki ko'rsatish mumkin.")}</p>
      <CodeBlock code={`img = client.images.generate(model="${imageModel}",
                            prompt="Tog' ustidagi quyosh chiqishi", n=1)
open("rasm.png", "wb").write(base64.b64decode(img.data[0].b64_json))`} />
      <p>{tr("Rasm so'rovi matnga qaraganda sekinroq (odatda 20-60 soniya).")} {imageModels.length
        ? <>{tr("Rasm yaratish hozircha")} {codeList(imageModels)} {tr(imageModels.length > 1 ? 'modellarida' : 'modelida')}.</>
        : <>{tr("Rasm yaratish va tahrirlash jadvalda")} <b>{tr("rasm yaratish")}</b> {tr("belgisi bor modellarda ishlaydi.")}</>}</p>

      <h3>{tr("Rasmni tahrirlash")}</h3>
      <div className="endpoint"><span className="verb">POST</span><code>/v1/images/edits</code></div>
      <p>{tr("Rasm yuborasiz va nima o'zgarishini yozasiz — natijada yangi rasm qaytadi. Standart OpenAI SDK'ning")} <code>images.edit</code> {tr("usuli ishlaydi (")}<code>mask</code> {tr("hozircha qo'llanmaydi).")}</p>
      {editModels.length > 0 && <p>{tr("Rasm tahrirlash hozircha")} {codeList(editModels)} {tr(editModels.length > 1 ? 'modellarida' : 'modelida')}.</p>}
      <CodeBlock code={`img = client.images.edit(model="${editModel}",
                        image=open("mushuk.png", "rb"),
                        prompt="Fonini dengiz sohiliga almashtir")
open("natija.png", "wb").write(base64.b64decode(img.data[0].b64_json))`} />
      <p>{tr("Chatda ham ishlaydi:")} <code>{editModel}</code> {tr("ga rasm biriktirib \"fonini o'zgartir\", \"rasmni tahrirla\" kabi ko'rsatma bersangiz, javobdagi")} <code>images</code> {tr("maydonida tahrirlangan rasm qaytadi.")}</p>

      <h3>{tr("Agent rejimi (vosita chaqirish)")}</h3>
      <p>{tr("Coding agent va subagentlar uchun standart OpenAI")} <code>tools</code> {tr("protokoli qo'llab-quvvatlanadi: model vosita chaqiradi, siz natijani")} <code>role: "tool"</code> {tr("xabari bilan qaytarasiz.")}</p>
      <CodeBlock code={`resp = client.chat.completions.create(
    model="${toolModel}",
    messages=[{"role": "user", "content": "main.py dagi xatoni top"}],
    tools=[{"type": "function", "function": {
        "name": "read_file",
        "description": "Fayl mazmunini o'qiydi",
        "parameters": {"type": "object",
                       "properties": {"path": {"type": "string"}},
                       "required": ["path"]}}}],
)
for call in resp.choices[0].message.tool_calls or []:
    print(call.function.name, call.function.arguments)`} />
      <p>{tr("Agent rejimi barcha modellarda emas — jadvaldagi")} <b>{tr("agent")}</b> {tr("belgisiga qarang. Imkoniyati yo'q model so'ralsa, so'rov xato bilan qaytadi.")}</p>

      <h3>{tr("Takroriy so'rovlar (kesh)")}</h3>
      <p>{tr("Bir xil so'rov qisqa muddat ichida takrorlansa, javob keshdan beriladi va ancha tez qaytadi. Javobda")} <code>usage.prompt_tokens_details.cached_tokens</code> {tr("maydoni va")} <code>X-Cluster-Cache</code> {tr("sarlavhasi keshdan kelganini ko'rsatadi. Kesh kaliti butun so'rovning (system prompt, tarix, biriktirmalar bilan) xeshi, shuning uchun boshqa so'rovning javobi qaytmaydi.")}</p>

      <h2 id="modellar">{tr("Modellar")}</h2>
      <p>{tr("Mavjud modellar ro'yxati va imkoniyatlari")} <code>GET /v1/models</code> {tr("da (kalit bilan). Har bir modelda quyidagi maydonlar bor:")} <code>context_window</code>, <code>max_output_tokens</code>, <code>capabilities</code> (<code>streaming</code>, <code>reasoning_levels</code>, <code>conditional_verification</code> {tr("va boshqalar),")} <code>health</code> {tr("(joriy holat:")} <code>up</code>/<code>down</code>/<code>unknown</code>), <code>maintenance</code> {tr("(rejali texnik ishlar),")} <code>data_processing</code> {tr("va")} <code>pricing</code>.</p>
      {data?.items?.length > 0 && (
        <div className="table-wrap"><table className="table table--models">
          <thead><tr><th>{tr("id")}</th><th>{tr("Kontekst")}</th><th>{tr("Imkoniyatlar")}</th><th>{tr("Fikrlash")}</th><th>{tr("Ma'lumot")}</th></tr></thead>
          <tbody>{data.items.map((x) => (
            <tr key={x.id}>
              <td className="nowrap"><code>{x.id}</code><div className="dim small">{tr(x.display_name)}</div></td>
              <td className="nowrap">{fmtNumber(x.context_window)} {tr("token")}</td>
              {/* Every model handles text; extra capabilities are appended ("—" looked like an empty cell) */}
              <td className="muted small">{['matn', ...DOC_CAPS.filter(([k]) => x.capabilities?.[k]).map(([, v]) => v)].join(', ')}</td>
              <td className="muted small">{tr(x.capabilities.reasoning_levels.length ? x.capabilities.reasoning_levels.join(', ') : tr("yo'q"))}</td>
              <td className="nowrap">{x.data_processing === 'third_party'
                ? <span className="tag tag--warn" title={tr("So'rov uchinchi tomon xizmati ishtirokida qayta ishlanadi")}>{tr("uchinchi tomon")}</span>
                : <span className="tag" title={tr("So'rov faqat bizning serverlarimizda qayta ishlanadi")}>{tr("maxfiy")}</span>}</td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
      <h3>{tr("Zaxira model")}</h3>
      <p>{tr("Ba'zi modellar yuklama juda yuqori bo'lganda so'rovni o'sha provayderning zaxira modelida bajarishi mumkin (administrator yoqgan bo'lsa). Bunday javobda")} <code>"{META_FIELD}": {'{'}"fallback_model": true{'}'}</code> {tr("maydoni (oqimda — oxirgi bo'lakda) va")} <code>X-SF-Fallback-Model: 1</code> {tr("sarlavhasi bo'ladi. Zaxira o'chiq bo'lsa, band model")} <code>503 model_busy</code> {tr("va")} <code>Retry-After</code> {tr("qaytaradi.")}</p>
      <h3>{tr("Tekshiruvli modellar")}</h3>
      <p><code>conditional_verification: true</code> {tr("bo'lgan modelda (shartlar")} <code>verification</code> {tr("maydonida) javob avval yoziladi, keyin ikkinchi model uni tekshirib (xato, hisob-kitob, til) tuzatadi va siz yakuniy javobni olasiz. Javob ~2-4 baravar sekinroq keladi. Tekshiruv bajarilganini javobdagi")} <code>"{META_FIELD}": {'{'}"verified": true{'}'}</code> {tr("maydoni va")} <code>X-SF-Verified</code> {tr("sarlavhasi ko'rsatadi.")}</p>
      <ul>
        <li>{tr("Tekshiruv ishlamasa (vaqt tugasa, xizmat javob bermasa), siz tekshirilmagan javobni")} <code>verified: false</code> {tr("belgisi bilan olasiz, xato emas. Bunday holatda narx tekshiruvsiz modelning narxiga teng bo'lishi mumkin (modelning sozlamasiga qarab).")}</li>
        <li>{tr("Funksiya chaqirish (")}<code>tools</code>{tr("), JSON rejimi, rasm kiritish va")} <code>n &gt; 1</code> {tr("so'rovlari tekshirilmaydi.")}</li>
        <li>{tr("Oqimli (stream) so'rovda javob tekshiruv tugagach bo'laklab yuboriladi.")}</li>
        <li>{tr("Bunday modellarda so'rov va javob mazmuni tekshirish uchun tashqi hamkor modelga yuboriladi (")}<code>data_processing: "third_party"</code>{tr("). Maxfiy ma'lumot yuborishdan oldin shuni hisobga oling. Matnlar bizda saqlanmaydi.")}</li>
      </ul>

      <h2 id="hisob">{tr("Modellar va hisob")}</h2>
      <div className="endpoint"><span className="verb">GET</span><code>/v1/models</code></div>
      <div className="endpoint"><span className="verb">GET</span><code>/v1/balance</code></div>
      <p><code>/v1/balance</code> {tr("kalit egasining hisobidagi qoldiqni so'mda qaytaradi. Ilovangizdan qoldiqni kuzatish uchun qulay.")}</p>

      {freeMode ? (
        <>
          <h2 id="narx">{tr("Narx")}</h2>
          <p>{tr("Xizmat hozircha hamkorlar uchun bepul: so'rovlar uchun pul yechilmaydi. Har bir so'rovning token soni panelda ko'rinadi,")} <code>X-SF-Cost</code> {tr("sarlavhasi esa 0 qaytaradi.")}</p>
        </>
      ) : (
        <>
      <h2 id="narx">{tr("Narx qanday hisoblanadi")}</h2>
      <p>{tr("Narx = kirish tokenlari x kirish narxi + chiqish tokenlari x chiqish narxi. Token soni modelning o'zi qaytargan")} <code>usage</code> {tr("maydonidan olinadi.")}</p>
      {m && <p>{tr("Masalan,")} {tr(m.display_name)} {tr("uchun: 1 000 kirish va 500 chiqish tokeni =")} {fmtNumber((1000 * m.price_in_per_1m + 500 * m.price_out_per_1m) / MICRO / 1_000_000, 2)} {tr("so'm.")}</p>}
      <ul>
        <li>{tr("So'rov boshlanishida eng ko'p sarflanishi mumkin bo'lgan summa vaqtincha band qilinadi, javobdan keyin haqiqiy summa yechiladi va ortig'i qaytadi.")}</li>
        <li>{tr("O'ylash tokenlari ham chiqish tokeni sifatida hisoblanadi, shuning uchun yuqori daraja bir necha barobar qimmatga tushadi.")}</li>
        <li>{tr("Daraja so'rovda")} <code>reasoning_effort</code> {tr("bilan beriladi (fikrlash yoqilgan modellarda). Berilmasa, kalitga qo'yilgan daraja, u ham bo'lmasa modelning standarti (")}<code>medium</code>{tr(") qo'llanadi.")}</li>
        <li>{tr("Hisob, mantiq va murakkab kod savollarida o'ylashsiz modellar xato qilishi mumkin: bunday savollar uchun")} {tr(thinking.length ? <>{tr("fikrlash yoqilgan modellardan (")}{codeList(thinking)}{tr(") foydalaning")}</> : tr("fikrlash yoqilgan modellardan foydalaning"))}{tr(". Oddiy matn va tarjima uchun arzonroq model yetarli.")}</li>
        <li>{tr("Fikrlash yoqilgan modelda")} <code>max_tokens</code> {tr("ni juda kichik qo'ymang (kamida 3000): tokenlar o'ylashga ketib, javob bo'sh qolishi mumkin.")}</li>
        <li>{tr("Xato yoki uzilish bo'lsa, pul yechilmaydi.")}</li>
      </ul>

        </>
      )}

      <h2 id="chegaralar">{tr("Chegaralar")}</h2>
      <ul>
        <li>{tr("Daqiqasiga")} {tr(limits?.default_rpm ?? 30)} {tr("ta so'rov (kalit bo'yicha). Oshsa 429 qaytadi.")}</li>
        <li>{tr("Bir kalitdan bir vaqtda 3 tagacha so'rov.")}</li>
        <li>{tr("Model bitta, so'rovlar navbat bilan bajariladi: band paytda so'rov biroz kutishi mumkin.")}</li>
        <li>{tr("Kalit uchun oylik sarf chegarasi qo'yish mumkin.")}</li>
      </ul>

      <h2 id="xatolar">{tr("Xatolar")}</h2>
      <p>{tr("Xato javobi OpenAI formatida:")} <code>{'{"error": {"message", "type", "code"}}'}</code></p>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>HTTP</th><th>{tr("code")}</th><th>{tr("Ma'nosi")}</th></tr></thead>
          <tbody>{ERRORS.map(([s, c, d]) => <tr key={s}><td><b>{tr(s)}</b></td><td><code>{c}</code></td><td className="muted">{tr(d)}</td></tr>)}</tbody>
        </table>
      </div>
    </div>
  )
  const layout = (
    <div className="docs-layout">
      <aside className="docs-aside">
        <nav className="docs-nav" aria-label={tr("Hujjat bo'limlari")}>
          {SECTIONS.map(([id, label, ico]) => (
            <a key={id} href={`#${id}`} className={active === id ? 'is-active' : ''}>{tr(ico)}{tr(label)}</a>
          ))}
        </nav>
        <div className="help-card">
          <span className="ico-chip ico-chip--sm"><IconHeadset /></span>
          <b>{tr("Yana savollaringiz bormi?")}</b>
          <p>{tr("Integratsiya yoki texnik savollar bo'yicha panel orqali bog'laning.")}</p>
          <a className="btn btn--small" href="#boshlash">{tr("Boshlashga qaytish")} <IconArrowRight /></a>
        </div>
      </aside>

      {tr(content)}

      <aside className="docs-toc">
        <div className="docs-toc__box">
          <h4>{tr("Ushbu sahifada")}</h4>
          {SECTIONS.map(([id, label]) => (
            <a key={id} href={`#${id}`} className={active === id ? 'is-active' : ''}>{tr(label)}</a>
          ))}
        </div>
        <div className="docs-toc__box">
          <h4>{tr("Tezkor ma'lumot")}</h4>
          <div className="quick-row">
            <span className="ico-chip ico-chip--sm"><IconLink /></span>
            <div className="who"><b>Base URL</b><span>{tr(base)}/v1</span></div>
            <CopyIcon text={`${base}/v1`} />
          </div>
          <div className="quick-row">
            <span className="ico-chip ico-chip--sm"><IconShield /></span>
            <div className="who"><b>{tr("Autentifikatsiya")}</b><span>Bearer token (sk-sf-...)</span></div>
          </div>
          <div className="quick-row">
            <span className="ico-chip ico-chip--sm"><IconZap /></span>
            <div className="who"><b>{tr("Moslik")}</b><span>{tr("OpenAI API formatiga 1:1 mos")}</span></div>
          </div>
          <div className="quick-row">
            <span className="ico-chip ico-chip--sm"><IconDoc /></span>
            <div className="who"><b>{tr("Kontent turi")}</b><span>application/json</span></div>
          </div>
        </div>
      </aside>
    </div>
  )
  return inApp ? layout : <div className="wrap" style={{ padding: '26px 20px 64px' }}>{tr(layout)}</div>
}
