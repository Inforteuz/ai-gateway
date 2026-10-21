// Terms of use.
//
// Like the privacy policy, this must match the actual behavior of the code.
// Places checked:
//   - billing: an amount is held at the start of a request and the actual
//     cost is charged after the response; nothing is charged if the model
//     returns an error (`gateway.py`);
//   - `abuse.py`: free credit once per IP; a key used from many places gets
//     rate-reduced;
//   - rate limit: per-key requests per minute, 429 with `Retry-After`.
import { useSite } from '../auth.js'
import { ContactSection, LegalPage, LegalSection } from '../components/Legal.jsx'

export default function Terms() {
  const site = useSite()
  return (
    <LegalPage
      title={['Foydalanish ', 'shartlari']}
      lead="Xizmatdan foydalanganingizda quyidagi shartlarga rozilik bildirgan hisoblanasiz."
    >

      <LegalSection num={1} title="Xizmat nima">
        <p>
          {site.brand_name} — OpenAI API bilan mos keluvchi shlyuz. Siz mavjud kodingizni
          o'zgartirmasdan, faqat manzil va kalitni almashtirib ishlatishingiz mumkin.
          Modellar ro'yxati, imkoniyatlari va narxlari katalog sahifasida ochiq turadi.
        </p>
      </LegalSection>

      <LegalSection num={2} title="Hisob va kalitlar">
        <ul>
          <li>Bir shaxs uchun bitta hisob. Soxta hisoblar orqali bepul kreditni qayta olish taqiqlanadi.</li>
          <li>
            API kalitingiz — sizning mas'uliyatingizda. Uni ommaviy repozitoriyga,
            frontend kodiga yoki boshqa odamga bermang. Kalit bilan qilingan har qanday
            so'rov sizning hisobingizdan yechiladi.
          </li>
          <li>
            Bitta kalit ko'p joydan ishlatilayotgani aniqlansa, uning tezligi avtomatik
            pasaytiriladi. Har bir foydalanuvchi uchun alohida kalit yarating.
          </li>
        </ul>
      </LegalSection>

      <LegalSection num={3} title="To'lov va hisob-kitob">
        <ul>
          <li>
            Narx token bo'yicha hisoblanadi: kirish tokenlari × kirish narxi + chiqish
            tokenlari × chiqish narxi. Joriy narxlar katalogda.
          </li>
          <li>
            So'rov boshida eng ko'p sarflanishi mumkin summa vaqtincha band qilinadi,
            javobdan keyin haqiqiy summa yechiladi va qolgani qaytariladi.
          </li>
          <li>
            Model javob bermasa yoki xato qaytarsa, <b>pul yechilmaydi</b>.
          </li>
          <li>
            Ro'yxatdan o'tganda beriladigan bepul kredit bir foydalanuvchiga bir marta
            beriladi va pulga aylantirilmaydi.
          </li>
        </ul>
      </LegalSection>

      <LegalSection num={4} title="Taqiqlangan foydalanish">
        <p>Xizmatdan quyidagilar uchun foydalanish mumkin emas:</p>
        <ul>
          <li>Qonunga xilof, zarar keltiruvchi yoki boshqalarning huquqini buzuvchi mazmun yaratish.</li>
          <li>Spam, firibgarlik yoki boshqa shaxs nomidan soxta xabar tarqatish.</li>
          <li>Xizmatga ataylab ortiqcha yuklama berish yoki chegaralarni aylanib o'tishga urinish.</li>
          <li>Kalitni qayta sotish yoki xizmatni o'z nomingizdan qayta sotish (alohida kelishuvsiz).</li>
        </ul>
        <p className="muted small">
          Bu shartlar buzilsa, hisob vaqtincha to'xtatilishi mumkin. Jiddiy bo'lmagan
          hollarda avval ogohlantiramiz.
        </p>
      </LegalSection>

      <LegalSection num={5} title="Tezlik chegaralari">
        <p>
          Har bir kalit uchun daqiqasiga so'rovlar soni cheklangan. Chegaradan oshsangiz
          javobda <code>429</code> kodi va <code>Retry-After</code> sarlavhasi qaytadi —
          mijozingiz shunga qarab kutib qayta yuborishi kerak. Qisqa portlashlarda so'rov
          rad etilmaydi, bir necha soniya kutiladi.
        </p>
      </LegalSection>

      <LegalSection num={6} title="Uzilishlar va kafolat">
        <p>
          Xizmat "qanday bo'lsa shundayligicha" taqdim etiladi. Biz uzluksiz ishlashga
          harakat qilamiz va uzilishlarni holat sahifasida ochiq ko'rsatamiz, lekin
          to'xtab qolish ehtimolini butunlay istisno qila olmaymiz. Shu sababli xizmat
          hayot va sog'liqqa bevosita ta'sir qiluvchi tizimlarda yagona tayanch sifatida
          ishlatilmasligi kerak.
        </p>
        <p className="muted small">
          Javoblarning to'g'riligi kafolatlanmaydi — sun'iy intellekt xato qilishi mumkin.
          Muhim qarorlarda javobni tekshiring.
        </p>
      </LegalSection>

      <LegalSection num={7} title="O'zgarishlar">
        <p>
          Narxlar va shartlar o'zgarishi mumkin. Narx oshsa yoki shartlar jiddiy
          o'zgarsa, oldindan xabar beramiz. Siyosat bilan rozi bo'lmasangiz, xizmatdan
          foydalanishni to'xtatishingiz va qolgan balansni qaytarishni so'rashingiz
          mumkin.
        </p>
      </LegalSection>
      <ContactSection num={8} />
    </LegalPage>
  )
}
