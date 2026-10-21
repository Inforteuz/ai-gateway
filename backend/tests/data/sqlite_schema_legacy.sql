-- SQLite davridagi sxema (git: c7cf881 backend/app/schema.sql + db.py MIGRATIONS + ish vaqtida yaratilgan jadvallar).
-- dbadmin import/export testlari uchun manba sifatida ishlatiladi.

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name          TEXT NOT NULL DEFAULT '',
  password_hash TEXT NOT NULL,
  role          TEXT NOT NULL DEFAULT 'user' CHECK (role IN ('user','admin')),
  status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','suspended')),
  created_at    INTEGER NOT NULL,
  last_login_at INTEGER
);

CREATE TABLE IF NOT EXISTS wallets (
  user_id    INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
  balance    INTEGER NOT NULL DEFAULT 0,   -- haqiqiy qoldiq
  held       INTEGER NOT NULL DEFAULT 0,   -- ishlayotgan so'rovlar uchun band qilingan summa
  updated_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
  id         TEXT PRIMARY KEY,             -- tokenning sha256 qiymati
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  ip         TEXT,
  user_agent TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS api_keys (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  prefix        TEXT NOT NULL,             -- kalitning ko'rsatiladigan boshi
  last4         TEXT NOT NULL,
  key_hash      TEXT NOT NULL UNIQUE,      -- sha256; kalitning o'zi saqlanmaydi
  status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
  rpm_limit     INTEGER,                   -- NULL = umumiy standart
  monthly_limit INTEGER,                   -- NULL = cheklovsiz (mikro-so'm)
  free          INTEGER NOT NULL DEFAULT 0, -- 1 = hisobdan pul yechilmaydi (faqat loglanadi)
  reasoning_effort TEXT,                    -- NULL = umumiy sozlama; none|low|medium|high|model
  created_at    INTEGER NOT NULL,
  last_used_at  INTEGER,
  revoked_at    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_keys_user ON api_keys(user_id);

CREATE TABLE IF NOT EXISTS models (
  id                 TEXT PRIMARY KEY,     -- mijoz ko'radigan nom
  upstream_name      TEXT NOT NULL,        -- Ollama'dagi nom
  display_name       TEXT NOT NULL,
  description        TEXT NOT NULL DEFAULT '',
  price_in_per_1m    INTEGER NOT NULL,     -- 1M kirish tokeni narxi (mikro-so'm)
  price_out_per_1m   INTEGER NOT NULL,     -- 1M chiqish tokeni narxi (mikro-so'm)
  context_window     INTEGER NOT NULL DEFAULT 32768,
  enabled            INTEGER NOT NULL DEFAULT 1,
  created_at         INTEGER NOT NULL,
  provider           TEXT NOT NULL DEFAULT 'local',  -- local = ishxonadagi Ollama; external = tashqi provayder
  refine             INTEGER NOT NULL DEFAULT 0,     -- 1 = javob tashqi model tomonidan tekshiriladi
  listed             INTEGER NOT NULL DEFAULT 1,     -- 0 = ro'yxatda ko'rinmaydi (lekin chaqirsa ishlaydi)
  default_reasoning  TEXT,                           -- NULL = umumiy sozlama
  fallback_price_from TEXT,                          -- tekshiruv ishlamasa, shu model narxi qo'llanadi
  allow_thinking     INTEGER NOT NULL DEFAULT 1      -- 0 = o'ylash (reasoning) har doim o'chiq, mijoz so'rasa ham
);

CREATE TABLE IF NOT EXISTS usage_logs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id        TEXT NOT NULL UNIQUE,
  user_id           INTEGER NOT NULL REFERENCES users(id),
  key_id            INTEGER REFERENCES api_keys(id),
  model             TEXT NOT NULL,
  endpoint          TEXT NOT NULL,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  cost              INTEGER NOT NULL DEFAULT 0,
  status_code       INTEGER NOT NULL,
  stream            INTEGER NOT NULL DEFAULT 0,
  estimated         INTEGER NOT NULL DEFAULT 0, -- 1 = token soni taxminiy hisoblangan
  latency_ms        INTEGER,
  ttfb_ms           INTEGER,
  error             TEXT,
  category          TEXT,                       -- NULL = muvaffaqiyatli; aks holda platform|billing|limits|client|cancelled
  reasoning         TEXT,                       -- so'rovga qo'llangan fikrlash darajasi
  refined           TEXT,                       -- tekshiruv natijasi: ok | fallback:<sabab> | partial:<sabab> (oqim o'rtasida uzilgan) | NULL
  refine_ms         INTEGER,
  ip                TEXT,
  created_at        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_usage_user_time ON usage_logs(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_key_time  ON usage_logs(key_id, created_at);
CREATE INDEX IF NOT EXISTS idx_usage_time      ON usage_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_usage_category  ON usage_logs(category, created_at);

-- Bosh kitob: faqat qo'shiladi, hech qachon o'zgartirilmaydi.
CREATE TABLE IF NOT EXISTS ledger (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id),
  kind          TEXT NOT NULL CHECK (kind IN ('topup','usage','adjustment','refund','bonus')),
  amount        INTEGER NOT NULL,          -- + kirim, - chiqim
  balance_after INTEGER NOT NULL,
  usage_id      INTEGER REFERENCES usage_logs(id),
  note          TEXT NOT NULL DEFAULT '',
  admin_id      INTEGER REFERENCES users(id),
  created_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON ledger(user_id, id);

CREATE TABLE IF NOT EXISTS topup_requests (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id      INTEGER NOT NULL REFERENCES users(id),
  amount       INTEGER NOT NULL,
  note         TEXT NOT NULL DEFAULT '',
  status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
  admin_note   TEXT NOT NULL DEFAULT '',
  created_at   INTEGER NOT NULL,
  resolved_at  INTEGER,
  resolved_by  INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_topup_status ON topup_requests(status, id);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- Model serveri (ishxonadagi kompyuter) holati: har 30 soniyada bitta yozuv.
CREATE TABLE IF NOT EXISTS health_checks (
  ts         INTEGER NOT NULL,
  state      TEXT NOT NULL,          -- up | tunnel_down | ollama_down
  latency_ms INTEGER,
  error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_health_ts ON health_checks(ts);

-- Uzilishlar (insidentlar): kamida 2 ta ketma-ket muvaffaqiyatsiz tekshiruvdan keyin ochiladi.
CREATE TABLE IF NOT EXISTS incidents (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at INTEGER NOT NULL,
  ended_at   INTEGER,
  reason     TEXT NOT NULL           -- tunnel_down | ollama_down
);
CREATE INDEX IF NOT EXISTS idx_incidents_start ON incidents(started_at);

-- Ommaviy "Sifat namunalari": haqiqiy so'rovlar natijasi (tahrirsiz), admin skripti bilan yangilanadi.
CREATE TABLE IF NOT EXISTS samples (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  position    INTEGER NOT NULL,
  category    TEXT NOT NULL,
  prompt      TEXT NOT NULL,
  fast_model  TEXT NOT NULL,
  fast_answer TEXT NOT NULL,
  fast_ms     INTEGER,
  pro_model   TEXT NOT NULL,
  pro_answer  TEXT NOT NULL,
  pro_ms      INTEGER,
  pro_verified INTEGER NOT NULL DEFAULT 0,
  created_at  INTEGER NOT NULL
);

ALTER TABLE users ADD COLUMN signup_ip TEXT;
ALTER TABLE users ADD COLUMN google_sub TEXT;
ALTER TABLE users ADD COLUMN telegram_id TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_google ON users(google_sub) WHERE google_sub IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_telegram ON users(telegram_id) WHERE telegram_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS password_resets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT NOT NULL UNIQUE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at    INTEGER,
    request_ip TEXT
);
CREATE INDEX IF NOT EXISTS idx_resets_user ON password_resets(user_id);

CREATE TABLE IF NOT EXISTS model_health (
    upstream   TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    since      INTEGER NOT NULL,
    last_check INTEGER NOT NULL,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS benchmark_results (
    id          INTEGER PRIMARY KEY,
    model       TEXT NOT NULL,
    kategoriya  TEXT NOT NULL,
    jami        INTEGER NOT NULL,
    togri       INTEGER NOT NULL,
    o_rtacha_ms INTEGER,
    yangilandi  INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS benchmark_uniq ON benchmark_results(model, kategoriya);
