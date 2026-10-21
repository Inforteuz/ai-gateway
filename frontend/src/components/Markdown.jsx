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

// Safety: strip HTML tags
const renderer = new marked.Renderer()
const linkRenderer = renderer.link
renderer.link = (href, title, text) => {
  const html = linkRenderer.call(renderer, href, title, text)
  return html.replace(/^<a /, '<a target="_blank" rel="nofollow noopener noreferrer" ')
}

marked.setOptions({
  renderer,
  highlight: (code, lang) => {
    if (lang && hljs.getLanguage(lang)) {
      try {
        return hljs.highlight(code, { language: lang }).value
      } catch {}
    }
    return code
  },
  langPrefix: 'hljs language-',
  breaks: true,
})

export function Markdown({ children }) {
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
      btn.textContent = 'Nusxa'
      btn.type = 'button'
      btn.onclick = () => {
        navigator.clipboard.writeText(block.textContent).then(() => {
          setCopied(idx)
          setTimeout(() => setCopied(null), 2000)
        })
      }
      pre.appendChild(btn)
      if (copied === idx) btn.textContent = 'Nusxalandi'
    })
  }, [children, copied])

  const html = marked.parse(children || '', { async: false })

  return <div ref={ref} className="markdown" dangerouslySetInnerHTML={{ __html: html }} />
}
