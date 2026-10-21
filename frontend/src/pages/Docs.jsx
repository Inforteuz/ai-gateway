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
const codeList = (ms) => ms.map((x, i) => <span key={x.id}>{i ? ', ' : ''}<code>{x.id}</code></span>)

export default function Docs({ inApp }) {
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
  const toolModel = pickModel(items, 'tool_calling')
  const limits = data?.limits
  const base = apiOrigin(site)
  const content = (
    <div className="prose">
      <nav className="crumbs" aria-label="Yo'l"><span>Hujjatlar</span><i>/</i><span>Boshlash</span></nav>
      <h1 className="docs-title">{site.brand_name} <em>hujjatlari</em></h1>
      <p>{site.brand_name} OpenAI formatiga mos. Mavjud kutubxonangizda faqat <code>base_url</code> va <code>api_key</code> ni almashtirsangiz yetarli.</p>
      <div className="docs-highlights">
        <div className="mini-card"><span className="mini-card__head"><span className="ico-chip ico-chip--sm"><IconZap /></span> OpenAI bilan 1:1 mos</span><span className="dim small">Mavjud SDK, prompt va kodlaringiz ishlaydi</span></div>
        <div className="mini-card"><span className="mini-card__head"><span className="ico-chip ico-chip--sm"><IconLayers /></span> Bir nechta model</span><span className="dim small">{site.assistant_name} modellari va kelajakda yangi imkoniyatlar</span></div>
        <div className="mini-card"><span className="mini-card__head"><span className="ico-chip ico-chip--sm"><IconShield /></span> Tez va ishonchli</span><span className="dim small">Barqaror infratuzilma va yuqori ishlash</span></div>
        <div className="mini-card"><span className="mini-card__head"><span className="ico-chip ico-chip--sm"><IconCode /></span> Oddiy integratsiya</span><span className="dim small">Bir necha daqiqada ishga tushiring</span></div>
      </div>

      <h2 id="boshlash">Boshlash</h2>
      <p>Panelning "API kalitlar" bo'limida kalit yarating, so'ng so'rov yuboring:</p>
      <CodeTabs tabs={snippets(base, 'sk-sf-...', model)} />

      <h2 id="autentifikatsiya">Autentifikatsiya</h2>
      <p>Har bir so'rovda kalit <code>Authorization</code> sarlavhasida yuboriladi. Kalitni brauzer kodiga yoki ommaviy repozitoriyga qo'ymang: u hisobingizdan pul sarflaydi.</p>
      <CodeBlock code={'Authorization: Bearer sk-sf-...'} />

      <h2 id="chat">Chat so'rovi</h2>
      <div className="endpoint"><span className="verb">POST</span><code>/v1/chat/completions</code></div>
      <ul>
        <li><code>model</code> (majburiy): {model}</li>
        <li><code>messages</code> (majburiy): <code>role</code> va <code>content</code> li ro'yxat</li>
        <li><code>max_tokens</code>: javobdagi tokenlarning yuqori chegarasi. Berilmasa {fmtNumber(limits?.default_max_tokens ?? 4096)}, eng ko'pi {fmtNumber(limits?.max_max_tokens ?? 16384)}</li>
        <li><code>stream</code>: <code>true</code> bo'lsa javob oqim bilan keladi (SSE)</li>
        <li><code>temperature</code>, <code>top_p</code>, <code>stop</code>, <code>tools</code> va boshqa standart parametrlar model tomoniga uzatiladi</li>
      </ul>
      <p>Matn davom ettirish uchun <code>POST /v1/completions</code> ham bor (<code>prompt</code> bilan).</p>
      <h3>Fikrlash (o'ylash) rejimi</h3>
      <p>{thinking.length ? <>O'ylash {codeList(thinking)} {thinking.length > 1 ? 'modellarida' : 'modelida'} yoqilgan</> : 'O\'ylash faqat ayrim modellarda yoqilgan'}: ular javob berishdan oldin o'ylaydi, shuning uchun murakkab hisob-kitob, mantiq va kodda aniqroq, lekin sezilarli sekinroq. Qolgan modellarda <code>reasoning_effort</code> yuborsangiz ham e'tiborsiz qoladi. Qaysi modelda qanday darajalar borligini <code>GET /v1/models</code> javobidagi <code>capabilities.reasoning_levels</code> ko'rsatadi.</p>
      <CodeBlock code={`resp = client.chat.completions.create(
    model="${thinkModel}",
    messages=[{"role": "user", "content": "Murakkab masalani bosqichma-bosqich yech: ..."}],
    extra_body={"reasoning_effort": "low"},  # none | low | medium | high (berilmasa medium)
)`} />
      <h3>Oqim (stream)</h3>
      <CodeBlock code={`stream = client.chat.completions.create(model="${model}", stream=True,
    messages=[{"role": "user", "content": "Salom!"}])
for chunk in stream:
    print(chunk.choices[0].delta.content or "", end="")`} />
      <p>Oqimda ham hisob token soniga qarab yuritiladi. Mijoz oqimni o'rtasida uzsa, shu paytgacha yaratilgan tokenlar uchun to'lanadi.</p>

      <h3>Rasm va fayl yuborish</h3>
      <p>Rasm yoki hujjatni OpenAI formatida <code>content</code> massivi orqali yuboriladi. Qaysi model nimani qabul qilishini quyidagi jadvaldagi <b>Imkoniyatlar</b> ustuni ko'rsatadi — imkoniyati yo'q modelga yuborilsa, so'rov xato bilan qaytadi (jim e'tiborsiz qoldirilmaydi).</p>
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
        <li>Rasm <code>data:</code> ko'rinishida yoki ochiq <code>https://</code> havola bo'lishi mumkin. Ichki tarmoq manzillari qabul qilinmaydi.</li>
        <li>Bitta fayl 12 MB gacha, bitta so'rovda jami 30 MB va ko'pi bilan 6 ta fayl.</li>
        <li>Qabul qilinadigan turlar: png, jpeg, webp, gif, bmp, pdf, txt, csv, md, json, doc(x), xls(x).</li>
        <li>Fayl biriktirilgan so'rov uzoqroq davom etadi, shuning uchun mijoz tomonidagi timeout'ni kamida 120 soniya qilib qo'ying.</li>
      </ul>

      <h3>Rasm yaratish</h3>
      <div className="endpoint"><span className="verb">POST</span><code>/v1/images/generations</code></div>
      <p>Natija har doim <code>b64_json</code> ko'rinishida qaytadi (havola emas), shuning uchun uni to'g'ridan-to'g'ri saqlash yoki ko'rsatish mumkin.</p>
      <CodeBlock code={`img = client.images.generate(model="${imageModel}",
                            prompt="Tog' ustidagi quyosh chiqishi", n=1)
open("rasm.png", "wb").write(base64.b64decode(img.data[0].b64_json))`} />
      <p>Rasm so'rovi matnga qaraganda sekinroq (odatda 20-60 soniya). {imageModels.length
        ? <>Rasm yaratish va tahrirlash hozircha {codeList(imageModels)} {imageModels.length > 1 ? 'modellarida' : 'modelida'}.</>
        : <>Rasm yaratish va tahrirlash jadvalda <b>rasm yaratish</b> belgisi bor modellarda ishlaydi.</>}</p>

      <h3>Rasmni tahrirlash</h3>
      <div className="endpoint"><span className="verb">POST</span><code>/v1/images/edits</code></div>
      <p>Rasm yuborasiz va nima o'zgarishini yozasiz — natijada yangi rasm qaytadi. Standart OpenAI SDK'ning <code>images.edit</code> usuli ishlaydi (<code>mask</code> hozircha qo'llanmaydi).</p>
      <CodeBlock code={`img = client.images.edit(model="${imageModel}",
                        image=open("mushuk.png", "rb"),
                        prompt="Fonini dengiz sohiliga almashtir")
open("natija.png", "wb").write(base64.b64decode(img.data[0].b64_json))`} />
      <p>Chatda ham ishlaydi: <code>{imageModel}</code> ga rasm biriktirib "fonini o'zgartir", "rasmni tahrirla" kabi ko'rsatma bersangiz, javobdagi <code>images</code> maydonida tahrirlangan rasm qaytadi.</p>

      <h3>Agent rejimi (vosita chaqirish)</h3>
      <p>Coding agent va subagentlar uchun standart OpenAI <code>tools</code> protokoli qo'llab-quvvatlanadi: model vosita chaqiradi, siz natijani <code>role: "tool"</code> xabari bilan qaytarasiz.</p>
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
      <p>Agent rejimi barcha modellarda emas — jadvaldagi <b>agent</b> belgisiga qarang. Imkoniyati yo'q model so'ralsa, so'rov xato bilan qaytadi.</p>

      <h3>Takroriy so'rovlar (kesh)</h3>
      <p>Bir xil so'rov qisqa muddat ichida takrorlansa, javob keshdan beriladi va ancha tez qaytadi. Javobda <code>usage.prompt_tokens_details.cached_tokens</code> maydoni va <code>X-Cluster-Cache</code> sarlavhasi keshdan kelganini ko'rsatadi. Kesh kaliti butun so'rovning (system prompt, tarix, biriktirmalar bilan) xeshi, shuning uchun boshqa so'rovning javobi qaytmaydi.</p>

      <h2 id="modellar">Modellar</h2>
      <p>Mavjud modellar ro'yxati va imkoniyatlari <code>GET /v1/models</code> da (kalit bilan). Har bir modelda quyidagi maydonlar bor: <code>context_window</code>, <code>max_output_tokens</code>, <code>capabilities</code> (<code>streaming</code>, <code>reasoning_levels</code>, <code>conditional_verification</code> va boshqalar), <code>health</code> (joriy holat: <code>up</code>/<code>down</code>/<code>unknown</code>), <code>maintenance</code> (rejali texnik ishlar), <code>data_processing</code> va <code>pricing</code>.</p>
      {data?.items?.length > 0 && (
        <div className="table-wrap"><table className="table table--models">
          <thead><tr><th>id</th><th>Kontekst</th><th>Imkoniyatlar</th><th>Fikrlash</th><th>Ma'lumot</th></tr></thead>
          <tbody>{data.items.map((x) => (
            <tr key={x.id}>
              <td className="nowrap"><code>{x.id}</code><div className="dim small">{x.display_name}</div></td>
              <td className="nowrap">{fmtNumber(x.context_window)} token</td>
              {/* Every model handles text; extra capabilities are appended ("—" looked like an empty cell) */}
              <td className="muted small">{['matn', ...DOC_CAPS.filter(([k]) => x.capabilities?.[k]).map(([, v]) => v)].join(', ')}</td>
              <td className="muted small">{x.capabilities.reasoning_levels.length ? x.capabilities.reasoning_levels.join(', ') : 'yo\'q'}</td>
              <td className="nowrap">{x.data_processing === 'third_party'
                ? <span className="tag tag--warn" title="So'rov uchinchi tomon xizmati ishtirokida qayta ishlanadi">uchinchi tomon</span>
                : <span className="tag" title="So'rov faqat bizning serverlarimizda qayta ishlanadi">maxfiy</span>}</td>
            </tr>
          ))}</tbody>
        </table></div>
      )}
      <h3>Zaxira model</h3>
      <p>Ba'zi modellar yuklama juda yuqori bo'lganda so'rovni o'sha provayderning zaxira modelida bajarishi mumkin (administrator yoqgan bo'lsa). Bunday javobda <code>"{META_FIELD}": {'{'}"fallback_model": true{'}'}</code> maydoni (oqimda — oxirgi bo'lakda) va <code>X-SF-Fallback-Model: 1</code> sarlavhasi bo'ladi. Zaxira o'chiq bo'lsa, band model <code>503 model_busy</code> va <code>Retry-After</code> qaytaradi.</p>
      <h3>Tekshiruvli modellar</h3>
      <p><code>conditional_verification: true</code> bo'lgan modelda (shartlar <code>verification</code> maydonida) javob avval yoziladi, keyin ikkinchi model uni tekshirib (xato, hisob-kitob, til) tuzatadi va siz yakuniy javobni olasiz. Javob ~2-4 baravar sekinroq keladi. Tekshiruv bajarilganini javobdagi <code>"{META_FIELD}": {'{'}"verified": true{'}'}</code> maydoni va <code>X-SF-Verified</code> sarlavhasi ko'rsatadi.</p>
      <ul>
        <li>Tekshiruv ishlamasa (vaqt tugasa, xizmat javob bermasa), siz tekshirilmagan javobni <code>verified: false</code> belgisi bilan olasiz, xato emas. Bunday holatda narx tekshiruvsiz modelning narxiga teng bo'lishi mumkin (modelning sozlamasiga qarab).</li>
        <li>Funksiya chaqirish (<code>tools</code>), JSON rejimi, rasm kiritish va <code>n &gt; 1</code> so'rovlari tekshirilmaydi.</li>
        <li>Oqimli (stream) so'rovda javob tekshiruv tugagach bo'laklab yuboriladi.</li>
        <li>Bunday modellarda so'rov va javob mazmuni tekshirish uchun tashqi hamkor modelga yuboriladi (<code>data_processing: "third_party"</code>). Maxfiy ma'lumot yuborishdan oldin shuni hisobga oling. Matnlar bizda saqlanmaydi.</li>
      </ul>

      <h2 id="hisob">Modellar va hisob</h2>
      <div className="endpoint"><span className="verb">GET</span><code>/v1/models</code></div>
      <div className="endpoint"><span className="verb">GET</span><code>/v1/balance</code></div>
      <p><code>/v1/balance</code> kalit egasining hisobidagi qoldiqni so'mda qaytaradi. Ilovangizdan qoldiqni kuzatish uchun qulay.</p>

      {freeMode ? (
        <>
          <h2 id="narx">Narx</h2>
          <p>Xizmat hozircha hamkorlar uchun bepul: so'rovlar uchun pul yechilmaydi. Har bir so'rovning token soni panelda ko'rinadi, <code>X-SF-Cost</code> sarlavhasi esa 0 qaytaradi.</p>
        </>
      ) : (
        <>
      <h2 id="narx">Narx qanday hisoblanadi</h2>
      <p>Narx = kirish tokenlari x kirish narxi + chiqish tokenlari x chiqish narxi. Token soni modelning o'zi qaytargan <code>usage</code> maydonidan olinadi.</p>
      {m && <p>Masalan, {m.display_name} uchun: 1 000 kirish va 500 chiqish tokeni = {fmtNumber((1000 * m.price_in_per_1m + 500 * m.price_out_per_1m) / MICRO / 1_000_000, 2)} so'm.</p>}
      <ul>
        <li>So'rov boshlanishida eng ko'p sarflanishi mumkin bo'lgan summa vaqtincha band qilinadi, javobdan keyin haqiqiy summa yechiladi va ortig'i qaytadi.</li>
        <li>O'ylash tokenlari ham chiqish tokeni sifatida hisoblanadi, shuning uchun yuqori daraja bir necha barobar qimmatga tushadi.</li>
        <li>Daraja so'rovda <code>reasoning_effort</code> bilan beriladi (fikrlash yoqilgan modellarda). Berilmasa, kalitga qo'yilgan daraja, u ham bo'lmasa modelning standarti (<code>medium</code>) qo'llanadi.</li>
        <li>Hisob, mantiq va murakkab kod savollarida o'ylashsiz modellar xato qilishi mumkin: bunday savollar uchun {thinking.length ? <>fikrlash yoqilgan modellardan ({codeList(thinking)}) foydalaning</> : 'fikrlash yoqilgan modellardan foydalaning'}. Oddiy matn va tarjima uchun arzonroq model yetarli.</li>
        <li>Fikrlash yoqilgan modelda <code>max_tokens</code> ni juda kichik qo'ymang (kamida 3000): tokenlar o'ylashga ketib, javob bo'sh qolishi mumkin.</li>
        <li>Xato yoki uzilish bo'lsa, pul yechilmaydi.</li>
      </ul>

        </>
      )}

      <h2 id="chegaralar">Chegaralar</h2>
      <ul>
        <li>Daqiqasiga {limits?.default_rpm ?? 30} ta so'rov (kalit bo'yicha). Oshsa 429 qaytadi.</li>
        <li>Bir kalitdan bir vaqtda 3 tagacha so'rov.</li>
        <li>Model bitta, so'rovlar navbat bilan bajariladi: band paytda so'rov biroz kutishi mumkin.</li>
        <li>Kalit uchun oylik sarf chegarasi qo'yish mumkin.</li>
      </ul>

      <h2 id="xatolar">Xatolar</h2>
      <p>Xato javobi OpenAI formatida: <code>{'{"error": {"message", "type", "code"}}'}</code></p>
      <div className="table-wrap">
        <table className="table">
          <thead><tr><th>HTTP</th><th>code</th><th>Ma'nosi</th></tr></thead>
          <tbody>{ERRORS.map(([s, c, d]) => <tr key={s}><td><b>{s}</b></td><td><code>{c}</code></td><td className="muted">{d}</td></tr>)}</tbody>
        </table>
      </div>
    </div>
  )
  const layout = (
    <div className="docs-layout">
      <aside className="docs-aside">
        <nav className="docs-nav" aria-label="Hujjat bo'limlari">
          {SECTIONS.map(([id, label, ico]) => (
            <a key={id} href={`#${id}`} className={active === id ? 'is-active' : ''}>{ico}{label}</a>
          ))}
        </nav>
        <div className="help-card">
          <span className="ico-chip ico-chip--sm"><IconHeadset /></span>
          <b>Yana savollaringiz bormi?</b>
          <p>Integratsiya yoki texnik savollar bo'yicha panel orqali bog'laning.</p>
          <a className="btn btn--small" href="#boshlash">Boshlashga qaytish <IconArrowRight /></a>
        </div>
      </aside>

      {content}

      <aside className="docs-toc">
        <div className="docs-toc__box">
          <h4>Ushbu sahifada</h4>
          {SECTIONS.map(([id, label]) => (
            <a key={id} href={`#${id}`} className={active === id ? 'is-active' : ''}>{label}</a>
          ))}
        </div>
        <div className="docs-toc__box">
          <h4>Tezkor ma'lumot</h4>
          <div className="quick-row">
            <span className="ico-chip ico-chip--sm"><IconLink /></span>
            <div className="who"><b>Base URL</b><span>{base}/v1</span></div>
            <CopyIcon text={`${base}/v1`} />
          </div>
          <div className="quick-row">
            <span className="ico-chip ico-chip--sm"><IconShield /></span>
            <div className="who"><b>Autentifikatsiya</b><span>Bearer token (sk-sf-...)</span></div>
          </div>
          <div className="quick-row">
            <span className="ico-chip ico-chip--sm"><IconZap /></span>
            <div className="who"><b>Moslik</b><span>OpenAI API formatiga 1:1 mos</span></div>
          </div>
          <div className="quick-row">
            <span className="ico-chip ico-chip--sm"><IconDoc /></span>
            <div className="who"><b>Kontent turi</b><span>application/json</span></div>
          </div>
        </div>
      </aside>
    </div>
  )
  return inApp ? layout : <div className="wrap" style={{ padding: '26px 20px 64px' }}>{layout}</div>
}
