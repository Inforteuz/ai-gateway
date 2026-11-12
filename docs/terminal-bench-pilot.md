# Terminal-Bench pilot (2026-11-12) — oraliq hisobot

Holat: **to'xtatilgan, yarim yo'lda** (Mac o'chirilishi sababli). Gateway xatolari tuzatildi
va testlangan; ball jadvali hali to'liq emas.

## Sozlama

- Docker: Colima (`--vm-type vz --vz-rosetta`, 4 CPU / 8 GB). Vazifa tasvirlari amd64, Rosetta orqali.
- Harness: `harbor` 0.25.0 (`uv tool install harbor`). Hozirgi rasmiy `terminal-bench/terminal-bench@4.1.0`
  — 8 soatlik ekspert vazifalar, pilotga mos emas; `terminal-bench@2.0` (89 vazifa) ishlatildi.
- Vazifalar (10): easy — `fix-git`, `overfull-hbox`, `cobol-modernization`, `prove-plus-comm`;
  medium — `openssl-selfsigned-cert`, `regex-log`, `log-summary-date-ranges`, `nginx-request-logging`,
  `sqlite-db-truncate`, `multi-source-data-merger`. Brauzer modellari uchun 5 talik qism:
  `fix-git openssl-selfsigned-cert regex-log log-summary-date-ranges multi-source-data-merger`.
- Barcha CLI'lar host'dagi proxy orqali (`bench/terminal-bench/proxy.py`):
  - haqiqiy kalitni faqat proxy o'qiydi (`~/.config/ai-gateway-bench/key`), konteynerlar vaqtinchalik token oladi;
  - brauzer modellari (sable-0.5/3.1/3.5/3.7/4.0): global lock, bitta so'rov, oldingisi tugaganidan >= 30 s keyin;
    3 ta ketma-ket captcha/429/server_busy -> model to'xtatiladi (pilot davomida bunday bo'lmadi);
  - lokal modellar: jami <= 2 parallel;
  - har so'rov loglanadi: status, davomiylik, tool-call soni, buzilgan tool JSON, xato hodisalari, stop sababi.
- Brauzer modellarida agent timeout x3 (pacing tufayli), lokallarda x1.
- Claude Code: `ANTHROPIC_DEFAULT_HAIKU_MODEL` brauzer yugurishlarida `sable-2.5` (brauzer yukini kamaytirish uchun).

## Topilgan va tuzatilgan gateway xatolari (branch `tbench-pilot`)

| # | Commit | Sabab | Ta'sir | Tuzatish |
|---|---|---|---|---|
| 1 | f1a52ba | Claude Code 2.1.x `messages` ichida `role:"system"` yuboradi | `/v1/messages` 400 "Each message needs role 'user' or 'assistant'" — Claude Code **har bir** vazifada birinchi so'rovda to'xtaydi (0%) | system-role xabarlar yuqori system matniga qo'shiladi; `thinking.type=adaptive` + `output_config.effort` -> `reasoning_effort` |
| 2 | f1a52ba | Codex 0.167 avval `GET /v1/responses` (WebSocket) | 404 -> har sessiyada 5 marta qayta urinish, keyin HTTP | GET/WebSocket `/v1/responses` -> **426**; Codex buni ko'rib darhol HTTP'ga o'tadi (codex-rs `client.rs`, `UPGRADE_REQUIRED`) |
| 3 | 2868b4c | Lokal modellar konteksti 16k; Claude Code tizim prompti + 12 tool ≈ 12–13k token | `context_length_exceeded` matni Claude Code'ga notanish -> sessiya tugaydi | `/v1/messages` endi `prompt is too long: N tokens > M maximum` beradi (Claude Code shu matnda compact qilib qayta yuboradi). Codex kod bo'yicha taniydi — o'zgarmadi |
| 4 | ffbf6d1 | Klaster: model `actions:` blokini ``` siz yozsa, `_BARE` regex birinchi bo'sh qatorda to'xtardi | `cmd: \|` ichidagi heredoc jim kesilib, fayl yarim yoziladi (sable-3.5 + Codex, fix-git) | bo'sh qatorlar blok ichida qoladi |
| 5 | ffbf6d1 | Klaster loop guard: "Process exited with code 1", "approval policy is Never; reject command" muvaffaqiyat deb sanalardi | tuzatilgan qayta urinish bloklanib, agent navbati "I did not repeat this action" bilan tugaydi | xato belgilari kengaytirildi (xato tomonga adashish faqat qayta urinishga ruxsat beradi) |

Testlar: `backend/tests/test_protocols.py` (+4), `backend/tests/test_terminal_bench_tools.py` (yangi, 5).
To'liq suite: 936 passed, 1 skipped.

Eslatma: pilot 1–3-tuzatishlarni proxy shimi bilan taqlid qildi (`BENCH_SHIMS=system-role,ws426`), chunki prod'da hali yo'q.
4–5 klaster tuzatishlari taqlid qilinmadi — brauzer natijalari ular deploy qilinmagan holatni ko'rsatadi.

## Natijalar (yarim, 9 sinov)

| CLI | model | o'tdi / yugurildi |
|---|---|---|
| Claude Code | sable-2.5 | 0 / 2 (cobol-modernization, fix-git) |
| Codex | sable-2.5 | 0 / 4 (cobol, fix-git, log-summary, multi-source) |
| Codex | sable-3.5 | 1 / 2 (log-summary-date-ranges ✓, fix-git ✗) |
| mini-swe-agent | sable-2.5 | 1 / 1 (fix-git ✓) |
| OpenCode | sable-2.5 | 0 / 1 (smoke, fix-git — agent tugagach osilib qoldi, 900 s timeout) |

Proxy statistikasi (pilot davomidagi barcha chaqiruvlar): buzilgan tool-call JSON — **0**;
`length`/kesilgan javob — **0**; 4xx — faqat #1 xato (Claude Code) va bitta lokal context xatosi; 5xx — prod
02:45–02:47 da 1–2 daqiqa 502/503 (deploy/restart), bitta oqim o'rtada uzildi.

## Muvaffaqiyatsizlik sinflari (hozircha)

- **Gateway/protokol** (tuzatildi): #1 (Claude Code'ning hamma sinovi), #2 (Codex'ning hamma sinovi — kechikish),
  #4 va #5 (sable-3.5 fix-git).
- **Infra**: prod restart paytida uzilgan oqim (Codex + sable-2.5 cobol) — 1 holat.
- **Model imkoniyati** (sable-2.5): vazifani bajarish o'rniga foydalanuvchiga ko'rsatma yozib to'xtash
  ("cherry-pick qiling…") — Claude Code, Codex, OpenCode'da fix-git; tekshirmasdan yakunlash (cobol).
  Bitta holatda gateway "reasoning loop" himoyasi 95 s fikrlashni to'xtatdi (Claude Code qayta urindi va davom etdi).
- **Model/CLI mos kelmasligi**: sable-3.5 Codex `exec_command` ga `justification` ni `sandbox_permissions`siz,
  keyin `require_escalated` bilan yubordi (approval=never'da rad etiladi).
- **CLI/harness**: OpenCode fix-git'da ishni tugatib chiqmadi (toza konteynerda takrorlanmadi — gateway sababi emas);
  Codex "Model metadata for `sable-x` not found" — mijoz tomonda `model_context_window` sozlanmasa, 16k
  modellarda o'zi compact qilmaydi.

## Gipoteza (o'lchanmagan)

- Lokal modellarga qo'shiladigan identity system qatori (`branding.with_identity`) agent promptidan oldin turadi
  ("You are Sable, the AI assistant…" vs "You are Claude Code…"). sable-2.5 ning "maslahat berib to'xtash"
  odatiga hissa qo'shishi mumkin. Tekshirish: tools bor so'rovlarda qatorni oxiriga ko'chirib A/B qilish.

## Qolgan ishlar (ertaga)

1. Branch `tbench-pilot` ni ko'rib chiqib deploy qilish (asosiy sessiya), keyin proxy'ni shimsiz ishga tushirish.
2. Lanelarni davom ettirish (`bench/terminal-bench/lane.sh`; tugagan job'lar o'tkazib yuboriladi):
   - lokal: Claude Code / Codex / OpenCode × sable-2.5, model-1.0 × 10 vazifa;
   - qo'shimcha CLI'lar (sable-2.5, 5 vazifa): mini-swe-agent, Terminus-2, Qwen Code, Goose, OpenHands, Kimi, Aider;
   - brauzer (30 s pacing, ketma-ket): sable-3.5 × 3 CLI × 5 vazifa, keyin sable-3.7 / sable-4.0.
3. Klaster tuzatishlari (#4, #5) deploydan keyin sable-3.5 fix-git ni qayta yugurish.
4. OpenCode osilishi takrorlansa — alohida tekshirish.
5. Yakuniy jadval va sinflar sonini shu faylga yozish.

Ishga tushirish (Mac):

```bash
colima start --cpu 4 --memory 8 --disk 80 --vm-type vz --vz-rosetta
harbor dataset download terminal-bench@2.0 -o <dataset-dir>
export BENCH_UPSTREAM=<gateway origin> BENCH_DATASET=<dataset-dir>/terminal-bench
bench/terminal-bench/start_proxy.sh     # token fayli: bench/terminal-bench/token (git'ga kirmaydi)
bench/terminal-bench/lane.sh <lane-file>
```

Xom loglar (repo'dan tashqarida): session notes papkasidagi `tbench-data/` (requests.jsonl, jobs.tgz).
