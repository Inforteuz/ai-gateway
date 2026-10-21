// Illustration in the sign-in page's left panel: dark cards and a green wave
// at the bottom. Purely decorative — no information, so hidden from screen readers.
export default function AuthArt() {
  const bars = [38, 62, 46, 78, 58, 92]
  return (
    <div className="auth-art" aria-hidden="true">
      <div className="auth-art__cards">
        <div className="auth-card auth-card--api">
          <span className="auth-card__dot" />
          <b>API</b>
          <span className="auth-card__line" />
          <span className="auth-card__line auth-card__line--short" />
        </div>

        <div className="auth-card auth-card--chart">
          <div className="auth-card__bars">
            {bars.map((h, i) => (
              <span key={i} style={{ height: `${h}%`, opacity: 0.45 + i * 0.11 }} />
            ))}
          </div>
        </div>

        <div className="auth-card auth-card--star">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
            <path d="m12 3 1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3Z" />
          </svg>
        </div>
      </div>

      <svg className="auth-art__wave" viewBox="0 0 400 90" preserveAspectRatio="none">
        <path d="M0 52c48 0 62-26 104-26s58 30 104 30 58-30 100-30 44 14 92 14v40H0Z" fill="url(#authWave)" />
        <path d="M0 52c48 0 62-26 104-26s58 30 104 30 58-30 100-30 44 14 92 14" fill="none" stroke="var(--em-400)" strokeWidth="2" opacity="0.9" />
        <defs>
          <linearGradient id="authWave" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--em-400)" stopOpacity="0.26" />
            <stop offset="100%" stopColor="var(--em-400)" stopOpacity="0" />
          </linearGradient>
        </defs>
      </svg>
    </div>
  )
}
