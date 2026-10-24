import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { useSite } from '../auth.js'
import { PageHead } from './ui.jsx'

// Shared layout for legal pages (privacy, terms).
// The text lives in the pages themselves; this file only provides structure and styling.

export function LegalPage({ eyebrow = tr("Huquqiy hujjat"), title, lead, children }) {
  useLocaleRefresh()
  return (
    <div className="wrap legal">
      <PageHead eyebrow={tr(eyebrow)} title={tr(title)} lead={tr(lead)} />
      <div className="legal-body">{children}</div>
    </div>
  )
}

/** Numbered section. `num` is the section number, `title` is the section heading. */
export function LegalSection({ num, title, children }) {
  useLocaleRefresh()
  return (
    <section className="legal-section" id={`bolim-${num}`}>
      <span className="legal-no" aria-hidden="true">{tr(num)}</span>
      <div className="legal-text">
        <h2>{tr(title)}</h2>
        {children}
      </div>
    </section>
  )
}

// Contact section, shared by both legal pages. No personal contact is hardcoded:
// whoever deploys this can add their own contact channel here.
export function ContactSection({ num }) {
  useLocaleRefresh()
  const site = useSite()
  return (
    <LegalSection num={num} title={tr("Biz bilan bog'lanish")}>
      <p> {tr("Savol, shikoyat yoki ma'lumotingizni o'chirish talabi bo'lsa,")} {tr(site.brand_name)} {tr("xizmati administratoriga yozing:")}{' '}
        <a href={site.site_url} target="_blank" rel="noreferrer">{site.site_url.replace(/^[a-z]+:\/\//i, '')}</a>
      </p>
      <p className="muted small"> {tr("Xabaringizga odatda bir ish kuni ichida javob beramiz.")} </p>
    </LegalSection>
  )
}
