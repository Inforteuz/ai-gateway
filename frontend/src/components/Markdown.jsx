import { translateText as tr, useLang as useLocaleRefresh } from '../i18n/index.jsx'
import { marked } from 'marked'
import hljs from 'highlight.js/lib/core'
import python from 'highlight.js/lib/languages/python'
import javascript from 'highlight.js/lib/languages/javascript'
import bash from 'highlight.js/lib/languages/bash'
import json from 'highlight.js/lib/languages/json'
import sql from 'highlight.js/lib/languages/sql'
import { useEffect, useRef, useState } from 'react'

hljs.registerLanguage('python', python)
hljs.registerLanguage('javascript', javascript)
hljs.registerLanguage('js', javascript)
hljs.registerLanguage('bash', bash)
hljs.registerLanguage('sh', bash)
hljs.registerLanguage('json', json)
hljs.registerLanguage('sql', sql)

const escapeHtml = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]))

// Only these URL schemes may appear in links/images (no javascript:, vbscript:, data:text/html...)
const safeUrl = (href, { image = false } = {}) => {
  const url = String(href || '').trim()
  if (/^(https?:|mailto:)/i.test(url) || /^[/#?.]/.test(url)) return url
  if (image && /^data:image\/(png|jpe?g|gif|webp);/i.test(url)) return url
  return ''
}

// Markdown here can come from model answers (Samples page), so raw HTML is never passed through:
// it is shown as text. Links open in a new tab without referrer/opener.
marked.use({
  breaks: true,
  renderer: {
    html(token) {
      return escapeHtml(token.text)
    },
    link(token) {
      const text = this.parser.parseInline(token.tokens)
      const href = safeUrl(token.href)
      if (!href) return text
      const title = token.title ? ` title="${escapeHtml(token.title)}"` : ''
      return `<a href="${escapeHtml(href)}"${title} target="_blank" rel="nofollow noopener noreferrer">${text}</a>`
    },
    image(token) {
      const src = safeUrl(token.href, { image: true })
      if (!src) return escapeHtml(token.text)
      return `<img src="${escapeHtml(src)}" alt="${escapeHtml(token.text)}" loading="lazy">`
    },
    code(token) {
      const lang = (token.lang || '').split(/\s/)[0]
      let body = escapeHtml(token.text)
      if (lang && hljs.getLanguage(lang)) {
        try { body = hljs.highlight(token.text, { language: lang }).value } catch {}
      }
      const cls = lang ? ` class="hljs language-${escapeHtml(lang)}"` : ' class="hljs"'
      return `<pre><code${cls}>${body}</code></pre>\n`
    },
  },
})

export function Markdown({ children }) {
  useLocaleRefresh()
  const ref = useRef(null)
  const [copied, setCopied] = useState(null)

  useEffect(() => {
    if (!ref.current) return
    // Add a "copy" button to every code block
    ref.current.querySelectorAll('pre > code').forEach((block, idx) => {
      const pre = block.parentElement
      if (pre.querySelector('.md-copy')) return
      const btn = document.createElement('button')
      btn.className = 'md-copy'
      btn.textContent = tr("Nusxa")
      btn.type = 'button'
      btn.onclick = () => {
        navigator.clipboard.writeText(block.textContent).then(() => {
          setCopied(idx)
          setTimeout(() => setCopied(null), 2000)
        })
      }
      pre.appendChild(btn)
      if (copied === idx) btn.textContent = tr("Nusxalandi")
    })
  }, [children, copied])

  const html = marked.parse(children || '', { async: false })

  return <div ref={ref} className="markdown" dangerouslySetInnerHTML={{ __html: html }} />
}
