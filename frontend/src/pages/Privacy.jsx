import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
// Privacy policy.
//
// Every statement on this page MUST match the actual behavior of the code:
// this is a legal document, not marketing copy. Sources checked:
//   - `users` table: email, name, password hash, signup IP,
//     last login time, google_sub / telegram_id;
//   - `api_keys`: only the sha256 hash (the key itself is not stored);
//   - `usage_logs`: model, token count, price, status code, latency, IP.
//     Request and response TEXT is not recorded (the `refined` column only
//     stores "ok" or "fallback:<reason>").
// If the code changes, this page must be updated too.
import { useSite } from '../auth.js'
import { ContactSection, LegalPage, LegalSection } from '../components/Legal.jsx'

export default function Privacy() {
  useLocaleRefresh()
  const site = useSite()
  return (
    <LegalPage
      title={[tr("Maxfiylik"), tr("siyosati")]}
      lead={tr("Bu hujjat {v0} xizmati qanday ma'lumot yig'ishini, uni nima uchun ishlatishini va nimani saqlamasligini tushuntiradi.", { v0: (site.brand_name) })}
    >

      <LegalSection num={1} title={tr("So'rov va javob matnlari saqlanmaydi")}>
        <p> {tr("Eng muhim qoida: siz API orqali yuborgan savol va olgan javob matni bizning ma'lumotlar bazamizga")} <b>{tr("yozilmaydi")}</b>{tr(". Biz faqat so'rov haqidagi texnik belgilarni saqlaymiz — qaysi model ishlatilgani, token soni, narxi, holat kodi, javob tezligi va so'rov kelgan IP manzil.")} </p>
        <p className="muted small"> {tr("Shu sababli biz sizning so'rovlaringiz mazmunini o'qiy olmaymiz, ularni qayta ko'ra olmaymiz va modellarni o'rgatish uchun ishlata olmaymiz.")} </p>
      <p>{tr("So‘rov hisoboti mijoz dasturi, loyiha va maqsad yorliqlari hamda vosita nomlarini saqlaydi. Promptlar, javoblar va vosita argumentlari saqlanmaydi.")}</p></LegalSection>

      <LegalSection num={2} title={tr("Hisobingiz haqida saqlanadigan ma'lumot")}>
        <ul>
          <li>{tr("Elektron pochta manzili va ismingiz.")}</li>
          <li> {tr("Parolingiz")} <b>{tr("asl holida saqlanmaydi")}</b> {tr("— faqat qaytarib bo'lmaydigan xeshi yoziladi. Google yoki Telegram orqali kirganingizda parol umuman yaratilmaydi.")} </li>
          <li>{tr("Ro'yxatdan o'tgan paytdagi IP manzil va oxirgi kirish vaqti.")}</li>
          <li>{tr("Hisobingiz balansi, to'ldirish so'rovlari va yechilgan summalar tarixi.")}</li>
        </ul>
      </LegalSection>

      <LegalSection num={3} title={tr("API kalitlari")}>
        <p>{tr("Kalitning xeshi va oxirgi to‘rt belgisi saqlanadi. Serverda shifrlash kaliti sozlangan bo‘lsa, kalitning shifrlangan nusxasi ham saqlanadi va egasiga qayta ko‘rsatilishi mumkin.")}</p>
      </LegalSection>

      <LegalSection num={4} title={tr("Google va Telegram orqali kirish")}>
        <p> {tr("Google orqali kirganingizda biz Google'dan faqat")} <b>{tr("elektron pochtangizni, ismingizni va profil identifikatoringizni")}</b> {tr("olamiz. Gmail xatlaringiz, Drive fayllaringiz yoki boshqa Google xizmatlaringizga kirish huquqini so'ramaymiz va ololmaymiz.")} </p>
        <p> {tr("Telegram orqali kirganingizda Telegram identifikatoringiz va ismingiz olinadi. Telegram yuborgan ma'lumot imzo orqali tekshiriladi — imzosi tasdiqlanmagan ma'lumot qabul qilinmaydi.")} </p>
        <p className="muted small"> {tr("Elektron pochtangiz avvaldan ro'yxatdan o'tgan bo'lsa, yangi hisob yaratilmaydi — mavjud hisobingizga ulanadi.")} </p>
      </LegalSection>

      <LegalSection num={5} title={tr("Hamkor xizmatlar")}>
        <p> {tr("Ba'zi modellarda javobni tayyorlash uchun so'rovingiz mazmuni hamkor xizmatga yuboriladi. Bunday hollarda matn")} <b>{tr("bizda saqlanmaydi")}</b>{tr(", lekin u hamkorning o'z shartlari asosida qayta ishlanadi. Qaysi modelda shundayligi har bir modelning sahifasida ochiq yozilgan — modelni tanlashdan oldin ko'rib chiqishingiz mumkin.")} </p>
      </LegalSection>

      <LegalSection num={6} title={tr("Cookie fayllari")}>
        <p> {tr("Saytda faqat")} <b>{tr("sessiya cookie")}</b> {tr("ishlatiladi — u sizni tizimga kirgan holatda ushlab turish uchun kerak. Reklama yoki kuzatuv (tracking) cookie'lari yo'q, uchinchi tomon analitikasi ulanmagan.")} </p>
      </LegalSection>

      <LegalSection num={7} title={tr("IP manzil nima uchun yoziladi")}>
        <p> {tr("IP manzil xavfsizlik uchun saqlanadi: u bitta kalitni ko'p joyga tarqatib yuborishni, bepul kreditni qayta-qayta olish uchun soxta hisob ochishni va xizmatga ortiqcha yuklama tushirishni aniqlashga yordam beradi. Boshqa maqsadda ishlatilmaydi.")} </p>
      </LegalSection>

      <LegalSection num={8} title={tr("Ma'lumotingiz kimga beriladi")}>
        <p> {tr("Hech kimga. Biz shaxsiy ma'lumotlaringizni")} <b>{tr("sotmaymiz")}</b>{tr(", reklama tarmoqlariga bermaymiz va uchinchi tomonga marketing uchun uzatmaymiz. Yagona istisno — qonun talab qilgan hollarda vakolatli organga berish majburiyati.")} </p>
      </LegalSection>

      <LegalSection num={9} title={tr("Saqlash muddati va o'chirish")}>
        <p> {tr("Hisobingiz ma'lumotlari u faol turguncha saqlanadi. Hisobingizni va unga bog'liq barcha ma'lumotni o'chirishni istasangiz, bizga murojaat qiling — o'chiramiz. So'rovlar tarixi (token soni va narx) buxgalteriya uchun ma'lum muddat saqlanishi mumkin, lekin unda so'rov matni bo'lmaydi.")} </p>
      </LegalSection>

      <LegalSection num={10} title={tr("O'zgarishlar")}>
        <p> {tr("Siyosat o'zgarsa, bu sahifa yangilanadi. Jiddiy o'zgarish bo'lsa, hisobingizdagi elektron pochta orqali xabar beramiz va bu sahifada sanani yangilaymiz.")} </p>
      </LegalSection>
      <ContactSection num={11} />
    </LegalPage>
  )
}
