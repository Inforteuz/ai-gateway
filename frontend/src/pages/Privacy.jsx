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
  const site = useSite()
  return (
    <LegalPage
      title={['Maxfiylik ', 'siyosati']}
      lead={`Bu hujjat ${site.brand_name} xizmati qanday ma'lumot yig'ishini, uni nima uchun ishlatishini va nimani saqlamasligini tushuntiradi.`}
    >

      <LegalSection num={1} title="So'rov va javob matnlari saqlanmaydi">
        <p>
          Eng muhim qoida: siz API orqali yuborgan savol va olgan javob matni bizning
          ma'lumotlar bazamizga <b>yozilmaydi</b>. Biz faqat so'rov haqidagi texnik
          belgilarni saqlaymiz — qaysi model ishlatilgani, token soni, narxi, holat kodi,
          javob tezligi va so'rov kelgan IP manzil.
        </p>
        <p className="muted small">
          Shu sababli biz sizning so'rovlaringiz mazmunini o'qiy olmaymiz, ularni qayta
          ko'ra olmaymiz va modellarni o'rgatish uchun ishlata olmaymiz.
        </p>
      </LegalSection>

      <LegalSection num={2} title="Hisobingiz haqida saqlanadigan ma'lumot">
        <ul>
          <li>Elektron pochta manzili va ismingiz.</li>
          <li>
            Parolingiz <b>asl holida saqlanmaydi</b> — faqat qaytarib bo'lmaydigan xeshi
            yoziladi. Google yoki Telegram orqali kirganingizda parol umuman yaratilmaydi.
          </li>
          <li>Ro'yxatdan o'tgan paytdagi IP manzil va oxirgi kirish vaqti.</li>
          <li>Hisobingiz balansi, to'ldirish so'rovlari va yechilgan summalar tarixi.</li>
        </ul>
      </LegalSection>

      <LegalSection num={3} title="API kalitlari">
        <p>
          Kalitning o'zi bizda <b>saqlanmaydi</b>. Yaratilgan paytda u bir marta
          ko'rsatiladi, bazaga esa faqat uning xeshi va oxirgi to'rt belgisi yoziladi.
          Kalitni yo'qotsangiz, biz uni tiklab bera olmaymiz — yangisini yaratish kerak
          bo'ladi.
        </p>
      </LegalSection>

      <LegalSection num={4} title="Google va Telegram orqali kirish">
        <p>
          Google orqali kirganingizda biz Google'dan faqat <b>elektron pochtangizni, ismingizni
          va profil identifikatoringizni</b> olamiz. Gmail xatlaringiz, Drive fayllaringiz yoki
          boshqa Google xizmatlaringizga kirish huquqini so'ramaymiz va ololmaymiz.
        </p>
        <p>
          Telegram orqali kirganingizda Telegram identifikatoringiz va ismingiz olinadi.
          Telegram yuborgan ma'lumot imzo orqali tekshiriladi — imzosi tasdiqlanmagan
          ma'lumot qabul qilinmaydi.
        </p>
        <p className="muted small">
          Elektron pochtangiz avvaldan ro'yxatdan o'tgan bo'lsa, yangi hisob yaratilmaydi —
          mavjud hisobingizga ulanadi.
        </p>
      </LegalSection>

      <LegalSection num={5} title="Hamkor xizmatlar">
        <p>
          Ba'zi modellarda javobni tayyorlash uchun so'rovingiz mazmuni hamkor xizmatga
          yuboriladi. Bunday hollarda matn <b>bizda saqlanmaydi</b>, lekin u hamkorning
          o'z shartlari asosida qayta ishlanadi. Qaysi modelda shundayligi har bir
          modelning sahifasida ochiq yozilgan — modelni tanlashdan oldin ko'rib
          chiqishingiz mumkin.
        </p>
      </LegalSection>

      <LegalSection num={6} title="Cookie fayllari">
        <p>
          Saytda faqat <b>sessiya cookie</b> ishlatiladi — u sizni tizimga kirgan holatda
          ushlab turish uchun kerak. Reklama yoki kuzatuv (tracking) cookie'lari yo'q,
          uchinchi tomon analitikasi ulanmagan.
        </p>
      </LegalSection>

      <LegalSection num={7} title="IP manzil nima uchun yoziladi">
        <p>
          IP manzil xavfsizlik uchun saqlanadi: u bitta kalitni ko'p joyga tarqatib
          yuborishni, bepul kreditni qayta-qayta olish uchun soxta hisob ochishni va
          xizmatga ortiqcha yuklama tushirishni aniqlashga yordam beradi. Boshqa maqsadda
          ishlatilmaydi.
        </p>
      </LegalSection>

      <LegalSection num={8} title="Ma'lumotingiz kimga beriladi">
        <p>
          Hech kimga. Biz shaxsiy ma'lumotlaringizni <b>sotmaymiz</b>, reklama tarmoqlariga
          bermaymiz va uchinchi tomonga marketing uchun uzatmaymiz. Yagona istisno —
          qonun talab qilgan hollarda vakolatli organga berish majburiyati.
        </p>
      </LegalSection>

      <LegalSection num={9} title="Saqlash muddati va o'chirish">
        <p>
          Hisobingiz ma'lumotlari u faol turguncha saqlanadi. Hisobingizni va unga bog'liq
          barcha ma'lumotni o'chirishni istasangiz, bizga murojaat qiling — o'chiramiz.
          So'rovlar tarixi (token soni va narx) buxgalteriya uchun ma'lum muddat saqlanishi
          mumkin, lekin unda so'rov matni bo'lmaydi.
        </p>
      </LegalSection>

      <LegalSection num={10} title="O'zgarishlar">
        <p>
          Siyosat o'zgarsa, bu sahifa yangilanadi. Jiddiy o'zgarish bo'lsa, hisobingizdagi
          elektron pochta orqali xabar beramiz va bu sahifada sanani yangilaymiz.
        </p>
      </LegalSection>
      <ContactSection num={11} />
    </LegalPage>
  )
}
