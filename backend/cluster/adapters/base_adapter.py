"""Common adapter interface for all AI web-chat providers.

Every platform (ChatGPT, DeepSeek, Qwen, etc.) inherits from this class and
sends prompts / reads answers by injecting JS into the Chromium page.

IMPORTANT: CDP is ALWAYS accessed through `evaluate`/`call` from the `..cdp`
module. Never use `ws.recv()` directly: browser events arrive on the same
channel as replies and shift the read (so another call's result gets parsed).
"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Sequence

from .. import cdp, logs
from .. import settings as cfg

log = logs.get("adapter")


@dataclass(frozen=True)
class Capabilities:
    """What a provider can do — the balancer picks workers based on this.

    Based on the site's actual UI, not on guesses: is there a file input,
    can chats be deleted, etc. When an unsupported capability is requested
    the request is NOT sent to this worker — otherwise the user sends an
    image and the model answers the text without ever seeing it.
    """
    tool_calling: bool = False    # follows the tool-calling protocol
    vision: bool = False          # attach and analyse images
    files: bool = False           # upload documents (pdf, docx, csv)
    image_gen: bool = False       # generate images
    temporary_chat: bool = False  # chat not saved to history
    delete_chats: bool = False    # delete old chats

# "New chat" URL of every provider.
# ChatGPT: the temporary-chat=true parameter — the chat is not saved to history, always clean (ideal).
# Others: the home URL usually opens a new chat. If a site loads the last chat instead, the
# adapter's own `reset_chat` must click the "New Chat" button.
_HOME_URLS: dict[str, str] = {
    "chatgpt": "https://chatgpt.com/?temporary-chat=true",
    "deepseek": "https://chat.deepseek.com/",
    "qwen": "https://chat.qwen.ai/",
    "genspark": "https://www.genspark.ai/",
    "deepai": "https://deepai.org/chat",
    "zai": "https://chat.z.ai/",
    "grok": "https://grok.com/",
    "chatbotapp": "https://chatbotappai.ai/",
    "chatbotchatapp": "https://chatbotchatapp.com/",
    "gemini": "https://gemini.google.com/app",
}

# Generic selector for finding the chat input field
INPUT_SELECTOR = 'textarea, [contenteditable="true"]'


# JS snippet that turns code blocks in the answer into ``` fences.
#
# Why: sites syntax-highlight code and add LINE NUMBERS, "Copy"/"Download"
# buttons and a language label next to it. `innerText` folds all of that into
# the code text — the answer comes out as "yaml 1 2 3 4 actions:". That breaks
# two things:
#   1) the code the user gets does not run (line numbers inside);
#   2) the tool-call block (YAML) cannot be parsed.
# So code blocks are replaced with clean text.
READ_TEXT_JS = r"""
    (root => {
        // IMPORTANT: text is read from the LIVE DOM, NOT from a clone.
        // `innerText` derives line breaks from the CSS layout; a detached
        // clone has no layout and the whole answer collapses onto one line.
        //
        // THE DOM IS NOT MODIFIED for good: code blocks and UI elements are
        // hidden temporarily, a clean ``` fence is added in place of the code,
        // the text is read and EVERYTHING is restored within the same
        // synchronous step. Page scripts (React) never see the intermediate
        // state. `pre.replaceWith(...)` used to be used here — it swapped out
        // React-managed nodes and could break the page mid-stream.
        const NL = String.fromCharCode(10);
        const NBSP = String.fromCharCode(160);
        const TICKS = String.fromCharCode(96, 96, 96);
        const LANG = /^[a-z0-9+#.-]{1,16}$/i;

        const HIDE = '[class*="thinking" i], [class*="thought" i], [class*="tool-status" i], '
                   + '[class*="status-card" i], [class*="reasoning" i], button, [role="button"], '
                   + '[data-markdown-copy="exclude"]';
        // Providers' code block wrappers (measured on the live DOM):
        //   ChatGPT  — [data-code-block="true"]: header (language + "Run") and
        //              CodeMirror `.cm-content[data-language]` > `.cm-line`.
        //              `innerText` doubles blank lines, so lines are taken
        //              from `textContent`.
        //   DeepSeek — .md-code-block: banner (language + "Copy" + "Download") and
        //              a clean `<pre>`.
        const BLOCK = '[data-code-block="true"], .md-code-block';

        const clean = lines => {
            lines = lines.map(l => l.split(NBSP).join(' ').replace(/[ \t]+$/, ''));
            while (lines.length && lines[lines.length - 1].trim() === '') lines.pop();
            while (lines.length && lines[0].trim() === '') lines.shift();
            return lines;
        };

        const fromBlock = block => {
            const cm = block.querySelector('.cm-content');
            if (cm) {
                const lines = Array.from(cm.querySelectorAll('.cm-line')).map(l => l.textContent || '');
                return { lang: cm.getAttribute('data-language') || '', lines };
            }
            const pre = block.querySelector('pre');
            if (!pre) return null;
            let lang = '';
            const banner = block.querySelector('.md-code-block-banner, [data-markdown-copy="exclude"]');
            if (banner) {
                const first = ((banner.innerText || '').split(NL)[0] || '').trim();
                if (LANG.test(first)) lang = first;
            }
            const code = pre.querySelector('code');
            const m = code && /language-([a-z0-9+#.-]+)/i.exec(code.className || '');
            if (m) lang = m[1];
            return { lang, lines: (pre.innerText || '').split(NL) };
        };

        const fromPre = pre => {
            let lines = (pre.innerText || '').split(NL);
            const code = pre.querySelector('code');
            const m = code && /language-([a-z0-9+#.-]+)/i.exec(code.className || '');
            let lang = m ? m[1] : '';
            // Leading language label ("yaml", "python", ...)
            const head = (lines[0] || '').trim();
            if (LANG.test(head) && !/^[0-9]+$/.test(head)
                    && (!lang || head.toLowerCase() === lang.toLowerCase())) {
                lang = lang || head;
                lines = lines.slice(1);
            }
            // A run of consecutive line numbers (1, 2, 3, ...)
            let n = 1;
            while (lines.length && lines[0].trim() === String(n)) {
                lines = lines.slice(1);
                n += 1;
            }
            // Number at the start of each line ("1    actions:")
            lines = lines.map(l => l.replace(/^[0-9]{1,4}(?=[ \t]{2,})/, ''));
            return { lang, lines };
        };

        const hidden = [];
        const added = [];
        const hide = el => { hidden.push([el, el.style.display]); el.style.display = 'none'; };
        try {
            // 1) Code blocks: the original is hidden and a clean fence is placed next to it
            const blocks = Array.from(root.querySelectorAll(BLOCK));
            const pres = Array.from(root.querySelectorAll('pre')).filter(p => !p.closest(BLOCK));
            for (const el of blocks.concat(pres)) {
                const got = el.tagName === 'PRE' ? fromPre(el) : fromBlock(el);
                if (!got) continue;
                const holder = document.createElement('pre');
                holder.style.whiteSpace = 'pre';
                holder.setAttribute('data-sf-holder', '1');
                holder.textContent = TICKS + got.lang + NL + clean(got.lines).join(NL) + NL + TICKS;
                el.after(holder);
                added.push(holder);
                hide(el);
            }

            // 2) Reasoning blocks, buttons and code headers
            for (const el of Array.from(root.querySelectorAll(HIDE))) {
                if (el.closest('[data-sf-holder]') || el.style.display === 'none') continue;
                hide(el);
            }

            return (root.innerText || '').split(NBSP).join(' ').trim();
        } finally {
            // 3) Restore the page to its original state
            for (const h of added) h.remove();
            for (const [el, d] of hidden.reverse()) el.style.display = d;
        }
    })
"""


class BaseAIAdapter(ABC):
    # `input[type=file]` selector for attaching files (set by the adapter).
    # Sites hide this input — its visibility is not checked.
    FILE_INPUT: Optional[str] = None
    IMAGE_INPUT: Optional[str] = None   # separate input that accepts images only
    # File attach methods — tried in order, the first one that WORKS is kept.
    # "input" — `DOM.setFileInputFiles`; "paste"/"drop" — dispatch a `File` object
    # built inside the page through an event (the only way for sites that do not
    # listen to the hidden input).
    UPLOAD_METHODS: tuple[str, ...] = ("input", "paste", "drop")

    caps = Capabilities()

    def __init__(self, name: str):
        self.name = name

    # --- File attachment -----------------------------------------------------

    async def attach_files(self, ws, paths: Sequence[str], images_only: bool = False,
                           timeout: float = 40.0) -> bool:
        """Attaches files to the chat input and waits for the upload.

        Three methods are tried in order (`UPLOAD_METHODS`) and the result is
        MEASURED after each one — making sure the site really accepted the file.
        Why one method is not enough (measured):
          - ChatGPT, DeepSeek: setting `input[type=file]` works;
          - Qwen: a file set on that input is NOT NOTICED (the site does not
            listen to `change`), but a `paste`/`drop` event uploads it.

        Returns: True — the upload to the site was CONFIRMED. False — no method
        worked (the caller must abort the request: if the file is not sent the
        model never sees it and the answer would be bogus).
        """
        if not paths:
            return True
        names = [os.path.basename(p) for p in paths]
        base = await self.upload_state(ws, names)
        pushed = False

        # The method that worked last time is tried first. Measured (ChatGPT): in a
        # regular (non-temporary) chat `input` waits 12 s with no result while `paste`
        # works in 4 s — trying `input` first every time wasted time.
        pref = getattr(self, "_upload_pref", None)
        methods = ([pref] if pref in self.UPLOAD_METHODS else []) + [
            m for m in self.UPLOAD_METHODS if m != pref]
        for method in methods:
            if method == "input":
                hint = self.IMAGE_INPUT if (images_only and self.IMAGE_INPUT) else self.FILE_INPUT
                selector = await cdp.file_input_selector(ws, hint)
                if not selector:
                    continue
                if not await cdp.set_file_input(ws, selector, list(paths)):
                    continue
            else:
                # Push the bytes to the page once — the following methods reuse them
                if not pushed:
                    for path, name in zip(paths, names):
                        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                        if not await cdp.push_file(ws, path, mime, name):
                            log.warning("%s: could not push file to the page: %s", self.name, name)
                            return False
                    pushed = True
                if not await cdp.inject_files(ws, method):
                    continue

            if await self.wait_upload(ws, len(paths), base=base, names=names, timeout=timeout):
                log.info("%s: file attached (%s, %d file(s))", self.name, method, len(paths))
                self._upload_pref = method
                if pushed:
                    await cdp.clear_file_payload(ws)
                return True
            log.warning("%s: method '%s' did not work", self.name, method)

        if pushed:
            await cdp.clear_file_payload(ws)
        log.warning("%s: could not attach the file (%d methods tried)",
                    self.name, len(self.UPLOAD_METHODS))
        return False

    async def upload_state(self, ws, names: Sequence[str]) -> dict:
        """Measures how many attachments are visible in the composer.

        Relies on SIGNALS THE SITE GIVES, not on selectors (measured):
          - attaching an image adds a new `<img>` to the composer
            (ChatGPT: `data:`, DeepSeek: blob, Qwen: the uploaded file URL);
          - attaching a document shows the file NAME in the composer text.
        So it keeps working when class names change — they differ on every
        deploy (measured: DeepSeek `img._9f130b7`).
        """
        js = """(() => {
            const names = %s;
            const ed = Array.from(document.querySelectorAll('textarea, [contenteditable="true"]'))
                .find(e => e.offsetParent);
            let root = ed;
            for (let i = 0; i < 8 && root && root.parentElement; i++) root = root.parentElement;
            const scope = root || document.body;
            let imgs = 0;
            for (const im of scope.querySelectorAll('img')) {
                const s = im.currentSrc || im.src || '';
                if (s) imgs++;
            }
            const text = scope.innerText || '';
            const named = names.filter(n => text.includes(n)).length;
            // The "Uploading..." LABEL is the most reliable signal: DeepSeek's file
            // chip shows it first and then turns into "TXT 68B". Without it the
            // attachment would count as "ready" as soon as it appeared and the
            // prompt would be sent before the upload finished — while the send
            // button is still disabled, so the request never went out (measured: DeepSeek).
            const busy = !!document.querySelector(
                'progress, [role="progressbar"], [class*="uploading" i], [class*="progress" i]')
                || /uploading|yuklanmoqda|загружа|processing file/i.test(text);
            return { imgs, named, busy };
        })()""" % json.dumps(list(names))
        try:
            st = await cdp.evaluate(ws, js, timeout=15)
        except Exception:
            st = None
        if not isinstance(st, dict):
            return {"imgs": 0, "named": 0, "busy": False}
        return {"imgs": int(st.get("imgs") or 0), "named": int(st.get("named") or 0),
                "busy": bool(st.get("busy"))}

    # Measured selectors: ChatGPT `aria-label="Remove <file name>"`,
    # Qwen `button.close-button[aria-label="Remove file"]`.
    ATTACHMENT_REMOVE = (
        'button[aria-label^="Remove" i], button[aria-label*="remove file" i], '
        '[class*="composer-attachment" i] button, button.close-button'
    )

    async def dismiss_overlays(self, ws) -> int:
        """Closes modal/overlay layers covering the page.

        WHY: when sites close a modal they sometimes leave its TRANSPARENT
        wrapper in the DOM. It is invisible but covers the whole page and
        swallows ALL clicks. Exactly this was observed: at the point above
        the send button `document.elementFromPoint` returned `ant-modal-wrap`
        instead of the button — so four different click methods all failed
        and the request silently never went out.

        First try closing with Escape (the site's own logic runs), then the
        remaining leftover wrappers are removed from the DOM.

        Returns: the number of blockers removed.
        """
        found = await cdp.evaluate(ws, """(() => {
            const sels = '[class*="modal-wrap" i], [class*="modal-mask" i], '
                       + '[class*="overlay" i], [class*="backdrop" i]';
            let n = 0;
            for (const e of document.querySelectorAll(sels)) {
                const r = e.getBoundingClientRect();
                // Only those covering most of the page matter
                if (r.width < innerWidth * 0.5 || r.height < innerHeight * 0.5) continue;
                if (getComputedStyle(e).pointerEvents === 'none') continue;
                n += 1;
            }
            return n;
        })()""", timeout=10) or 0
        if not found:
            return 0

        await cdp.key(ws, "Escape", "Escape", 27)
        await asyncio.sleep(0.5)

        removed = await cdp.evaluate(ws, """(() => {
            const sels = '[class*="modal-wrap" i], [class*="modal-mask" i], '
                       + '[class*="overlay" i], [class*="backdrop" i]';
            let n = 0;
            for (const e of document.querySelectorAll(sels)) {
                const r = e.getBoundingClientRect();
                if (r.width < innerWidth * 0.5 || r.height < innerHeight * 0.5) continue;
                if (getComputedStyle(e).pointerEvents === 'none') continue;
                // If Escape did not close it — a leftover wrapper. With no visible
                // content inside it can be safely removed.
                const text = (e.innerText || '').trim();
                if (text.length > 40) continue;   // a real dialog with content — leave it
                e.remove();
                n += 1;
            }
            return n;
        })()""", timeout=10) or 0
        if removed:
            log.info("%s: removed %d blocker(s) covering the page",
                     self.name, removed)
            await asyncio.sleep(0.3)
        return int(removed)

    async def clear_composer_text(self, ws, selector: Optional[str] = None) -> int:
        """Clears text left over in the input field.

        WHY IT IS MANDATORY: if a request fails to send (button did not work,
        timeout, disconnect) the text STAYS in the field. The next request's
        text is APPENDED to it — one user's question sticks to another's and
        the model reads both as one request. Exactly this was observed:
        121 characters were left over and with the new prompt it became 160.

        Returns: the number of characters cleared.
        """
        sel = selector or 'textarea, [contenteditable="true"]'
        js = """(() => {
            const vis = e => { const r = e.getBoundingClientRect();
                return r.width > 40 && r.height > 5 && e.offsetParent !== null; };
            const el = Array.from(document.querySelectorAll(%s)).filter(vis).pop();
            if (!el) return 0;
            const before = (el.value ?? el.innerText ?? '').length;
            if (before === 0) return 0;
            el.focus();
            if (el.tagName === 'TEXTAREA' || el.tagName === 'INPUT') {
                const setter = Object.getOwnPropertyDescriptor(
                    Object.getPrototypeOf(el), 'value').set;
                setter.call(el, '');
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
            } else {
                const range = document.createRange();
                range.selectNodeContents(el);
                const s = window.getSelection();
                s.removeAllRanges(); s.addRange(range);
                document.execCommand('delete');
                el.dispatchEvent(new Event('input', { bubbles: true }));
            }
            return before;
        })()""" % repr(sel)
        try:
            n = int(await cdp.evaluate(ws, js, timeout=10) or 0)
        except Exception:
            return 0
        if n:
            log.info("%s: cleared %d leftover character(s) from the input field",
                     self.name, n)
        return n

    async def clear_attachments(self, ws, timeout: float = 8.0) -> int:
        """Removes attachments left over in the input field.

        WHY IT IS MANDATORY: if a request fails to send (timeout, error,
        disconnect) the file STAYS in the composer. The next request sends
        that file along with it — one user's document ends up in another
        user's request. Some sites also refuse new requests entirely while
        an attachment is pending.

        Returns: the number of attachments removed.
        """
        js = """(() => {
            const vis = e => { const r = e.getBoundingClientRect();
                return r.width > 4 && r.height > 4 && e.offsetParent !== null; };
            // Find the composer: walk up from the last visible input field.
            // Search only within that scope — otherwise other "close" buttons
            // on the page (modal, banner) could get clicked.
            const input = Array.from(
                document.querySelectorAll('textarea, [contenteditable="true"]')
            ).filter(vis).pop();
            if (!input) return 0;
            let box = input;
            for (let i = 0; i < 6 && box.parentElement; i++) box = box.parentElement;

            let removed = 0;
            // Each click removes one chip, so repeat
            for (let pass = 0; pass < 8; pass++) {
                const btn = Array.from(box.querySelectorAll(%s)).filter(vis)[0];
                if (!btn) break;
                btn.click();
                removed += 1;
            }
            return removed;
        })()""" % repr(self.ATTACHMENT_REMOVE)
        try:
            n = await cdp.evaluate(ws, js, timeout=timeout)
        except Exception as e:
            log.debug("%s: could not clear attachments: %s", self.name, e)
            return 0
        n = int(n or 0)
        if n:
            log.info("%s: removed %d leftover attachment(s) from the composer",
                     self.name, n)
            await asyncio.sleep(0.5)
        return n

    async def wait_upload(self, ws, count: int, base: Optional[dict] = None,
                          names: Sequence[str] = (), timeout: float = 40.0) -> bool:
        """Waits until the file has been uploaded to the site.

        Signal: an attachment appearing in the composer (`upload_state`) relative
        to the INITIAL state. No guessing — if no signal appears at all it returns
        False after `PROBE` seconds and `attach_files` moves to the next method
        (previously the full timeout was burned here for every file).
        """
        PROBE = 12.0          # time to decide that a method did not work
        SETTLE = 4.0          # wait for the rest after the first attachment
        base = base or {"imgs": 0, "named": 0}
        started = time.time()
        deadline = started + timeout
        seen_at = 0.0
        while time.time() < deadline:
            await asyncio.sleep(0.7)
            st = await self.upload_state(ws, names)
            progress = max(0, st["imgs"] - base.get("imgs", 0)) + st["named"]
            if progress and not seen_at:
                seen_at = time.time()
            if progress >= count and not st["busy"]:
                return True
            if not seen_at and (time.time() - started) > PROBE:
                return False
            # Some appeared and the upload finished — good enough
            if seen_at and not st["busy"] and (time.time() - seen_at) > SETTLE:
                return True
        log.warning("%s: timed out waiting for file upload (%ss)", self.name, int(timeout))
        return bool(seen_at)

    # --- Send confirmation --------------------------------------------------

    async def editor_text(self, ws, timeout: float = 10.0) -> str:
        """Current text in the input field."""
        js = """(() => {
            const ed = Array.from(document.querySelectorAll('textarea, [contenteditable="true"]'))
                .find(e => e.offsetParent) || document.querySelector('textarea');
            if (!ed) return '';
            return (ed.value !== undefined && ed.value !== null) ? ed.value : (ed.innerText || '');
        })()"""
        try:
            return str(await cdp.evaluate(ws, js, timeout=timeout) or "")
        except Exception:
            return ""

    async def wait_sent(self, ws, timeout: float = 8.0) -> bool:
        """Checks that the prompt was ACTUALLY sent.

        Signal: the input field becomes empty. Why — the send button is disabled
        while a file uploads and the click is ignored; without confirmation the
        full timeout is burned waiting for an answer to a request that was never
        sent (measured: DeepSeek, 110 seconds, "empty answer").
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            if len((await self.editor_text(ws)).strip()) < 3:
                return True
        return False

    # --- Image generation result ---------------------------------------------

    async def read_images(self, ws, timeout: float = 15.0) -> list[str]:
        """Returns the image URLs in the last answer (the generation result).

        Default implementation: the `src` values of `<img>` elements in the last
        assistant message. Very small images are dropped to skip avatars/icons.
        """
        js = """(() => {
            const imgs = Array.from(document.querySelectorAll('img'));
            return imgs.filter(i => {
                const r = i.getBoundingClientRect();
                if (r.width < 120 || r.height < 120) return false;
                // An image the user sent (for editing) is not a result
                if (i.closest('[data-message-author-role="user"], [data-turn="user"], form')) return false;
                const s = i.currentSrc || i.src || '';
                return s.startsWith('http') || s.startsWith('blob:') || s.startsWith('data:image');
            }).map(i => i.currentSrc || i.src).slice(-4);
        })()"""
        try:
            out = await cdp.evaluate(ws, js, timeout=timeout)
        except Exception:
            return []
        return [u for u in (out or []) if isinstance(u, str)]

    async def download_images(self, ws, urls: Sequence[str]) -> list[tuple[str, str]]:
        """Converts image URLs to (mime, base64) pairs.

        A `blob:` or cookie-protected link cannot be returned to the client — it
        will not open from outside. So the bytes are read inside the page and
        embedded in the response as base64.
        """
        out: list[tuple[str, str]] = []
        for u in urls:
            if u.startswith("data:image"):
                head, _, payload = u.partition(",")
                if payload:
                    out.append((head[5:].split(";")[0] or "image/png", payload))
                continue
            got = await cdp.fetch_as_base64(ws, u)
            if got:
                out.append(got)
        return out

    # --- Chat history management ----------------------------------------------

    async def delete_old_chats(self, ws, keep: int = 0) -> int:
        """Deletes old chats, keeping the newest `keep`.

        Default: does nothing (the adapter implements it).
        Returns: the number of chats deleted.
        """
        return 0

    @abstractmethod
    async def send_prompt(self, ws, prompt_text: str) -> bool:
        """Types the text into the browser page and clicks the send button."""

    @abstractmethod
    async def read_response(self, ws, timeout: int = 45) -> Optional[str]:
        """Collects the answer from the page. Waits for generation to finish and returns the stable text."""

    async def wait_for_input(self, ws, timeout: float = 10.0,
                             selector: str = INPUT_SELECTOR) -> bool:
        """Waits for a VISIBLE chat input field to appear.

        Checking visibility matters: some sites (e.g. ChatGPT) keep a hidden
        duplicate editor in the DOM. Checking mere presence would report
        "ready" before the page has loaded and the prompt would land in the
        hidden field.
        """
        js = """(() => {
            const els = Array.from(document.querySelectorAll(%s));
            return els.some(e => {
                const r = e.getBoundingClientRect();
                return r.width > 50 && r.height > 5 && e.offsetParent !== null;
            });
        })()""" % repr(selector)
        # SPEED (measured): previously it waited 0.5 s before every check and another
        # 0.5 s after a hit — adding exactly 1.0 s to every request on every provider.
        # Now it checks immediately; 0.2 s is enough for the framework state.
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if await cdp.evaluate(ws, js, timeout=5):
                    await asyncio.sleep(0.2)
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.25)
        return False

    async def goto(self, ws, url: str) -> None:
        """Navigates the page to another URL.

        Uses `location.href` instead of `Page.navigate`: it stays within the same
        CDP target, so the existing connection (`ws_url`) does not go stale.
        The evaluate reply may never arrive once navigation starts — that is
        normal, hence the short timeout and the swallowed error.
        """
        js = f"(() => {{ location.href = {url!r}; return true; }})()"
        try:
            await cdp.evaluate(ws, js, timeout=5)
        except Exception:
            pass  # navigation cut off the reply — expected

    async def reset_chat_for_images(self, ws, timeout: float = 10.0) -> None:
        """New chat for an image generation/editing request.

        Default: plain `reset_chat`. A provider that does not draw images in a
        temporary chat (ChatGPT) overrides this to open a regular chat.
        """
        await self.reset_chat(ws, timeout=timeout)

    async def finished_without_image(self, ws) -> bool:
        """True if the answer is finished and clearly has no image (e.g. the model refused).

        Default: unknown — waiting for the image continues for the full timeout.
        """
        return False

    async def generation_done(self, ws) -> bool:
        """Whether answer generation has finished (no stop button). Unknown — False."""
        return False

    async def after_image_response(self, ws) -> None:
        """Post-processing after an image answer (e.g. recording the chat in the registry)."""
        return None

    async def reset_chat(self, ws, timeout: float = 10.0) -> None:
        """Switches to a new chat before every request — users must not mix in one chat.

        Default implementation: navigates to the provider's home URL.
        If the site works through a "New Chat" button instead of a URL, the adapter can override this.
        """
        url = _HOME_URLS.get(self.name)
        if not url:
            return
        # Unique timestamp to bypass the cache — a clean load every time
        sep = "&" if "?" in url else "?"
        await self.goto(ws, f"{url}{sep}_sf={int(time.time() * 1000)}")
        # Continue even on timeout — send_prompt handles its own failure
        await self.wait_for_input(ws, timeout=timeout)

    # --- Model variant (model choice within a provider) ----------------------------
    # Default: the provider has no choice. Adapters such as Qwen override
    # `select_variant`. `variant`: None — main model, "fallback" — backup, any other
    # value — an exact model name (internal measurements only, the gateway never sends it).
    def has_variant(self, variant: str) -> bool:
        return False

    async def select_variant(self, ws, variant: Optional[str]) -> Optional[str]:
        """Selects the requested model. Returns: the active model name (for logging) or None."""
        return None

    # Bot check (captcha) markers: Alibaba "nc"/"baxia" slider (Qwen), Cloudflare
    # challenge and generic texts. Matches inside chat answers and code blocks are
    # ignored — there the word "slider" may be ordinary content.
    CHALLENGE_JS = r"""(() => {
        const vis = e => !!(e.offsetWidth || e.offsetHeight);
        const inContent = e => !!e.closest('.monaco-editor, pre, code, .markdown, .ds-markdown, .qwen-markdown, '
            + '.qwen-chat-message, .chat-response-message-render-flow, [data-message-author-role], [data-markdown-text-style]');
        const sel = '#nc_1_wrapper, .nc-container, .nc_wrapper, [id^="nc_"][id$="_wrapper"], #baxia-dialog-content, '
            + '.baxia-dialog, iframe[src*="captcha"], iframe[src*="baxia"], iframe[src*="challenges.cloudflare.com"], '
            + 'iframe[title*="challenge" i], #challenge-form, .cf-turnstile';
        const el = [...document.querySelectorAll(sel)].find(e => vis(e) && !inContent(e));
        if (el) return 'el:' + String(el.id || el.className || el.tagName).slice(0, 40);
        const re = /(slide to verify|drag the slider|please slide|slide to complete|verify you are human|unusual traffic|滑动验证|拖动滑块|请按住滑块|подтвердите, что вы человек)/i;
        const t = [...document.querySelectorAll('div, span, p, h1, h2, h3, label')]
            .find(e => e.children.length === 0 && vis(e) && !inContent(e) && re.test(e.innerText || ''));
        return t ? 'text:' + t.innerText.trim().slice(0, 40) : null;
    })()"""

    async def reload_after_challenge(self, ws) -> Optional[str]:
        """After a captcha: pause + one cache-bypassing reload. Returns: the marker if the captcha remains."""
        await asyncio.sleep(cfg.CHALLENGE_RELOAD_PAUSE)
        try:
            await cdp.call(ws, "Page.reload", {"ignoreCache": True})
        except Exception:
            pass
        await asyncio.sleep(3)
        try:
            await self.wait_for_input(ws, timeout=20)
        except Exception:
            pass
        return await self.detect_challenge(ws)

    async def detect_challenge(self, ws) -> Optional[str]:
        """Whether the page shows a bot check. Returns: a marker (for logging) or None."""
        try:
            hit = await cdp.evaluate(ws, self.CHALLENGE_JS, timeout=8)
        except Exception:
            return None
        return hit if isinstance(hit, str) and hit else None

    async def set_effort(self, ws, effort: Optional[str]) -> Optional[str]:
        """The provider's reasoning effort (if any). Returns: the effective level or None."""
        return None

    async def is_fresh_chat(self, ws) -> bool:
        """Whether the page is a clean chat with NO messages (for a pre-warmed page).

        Default — False: a pre-warmed page is not used until the provider supplies
        an exact check (so we never land in a previous user's conversation).
        """
        return False

    async def check_session(self, ws, timeout: float = 5.0,
                            recover: bool = True) -> tuple[bool, str]:
        """Whether the session is alive — checked WITHOUT sending a prompt (no quota used).

        Only inspects the DOM: is there a chat input field and no text on the
        page asking to log in. The check is cheap, so it can run regularly in
        the background.

        Returns: (healthy, reason).
        """
        script = """(() => {
            const input = document.querySelector('textarea, [contenteditable="true"]');
            const body = (document.body.innerText || '').toLowerCase();
            const loginHints = [
                'sign in to continue', 'please sign in', 'please log in',
                'log in to continue', 'session expired', 'sign up to continue',
                'create an account to'
            ];
            // On a page WITH an input field the login text may come from a previous answer
            // (e.g. code of a login page) — login is only reported when there is no field.
            const needsLogin = !input && loginHints.some(h => body.includes(h));
            return {
                hasInput: !!input,
                needsLogin: needsLogin,
                url: location.href.slice(0, 120)
            };
        })()"""
        try:
            val = await cdp.evaluate(ws, script, timeout=timeout)
        except Exception as e:
            return False, f"probe_failed:{type(e).__name__}"

        if not isinstance(val, dict):
            return False, "probe_bad_result"
        # Bot check: pause + ONE cache-bypassing reload (the owner's decision);
        # if it remains — "captcha" (the worker rests, the operator is notified). Sliders are never solved.
        if await self.detect_challenge(ws):
            log.warning("%s: bot check — pausing and doing one cache-bypassing reload", self.name)
            if await self.reload_after_challenge(ws):
                return False, "captcha"
            return await self.check_session(ws, timeout=timeout, recover=False)
        if val.get("needsLogin"):
            return False, "login_required"
        if not val.get("hasInput"):
            # No input field but no login text either — usually the page itself
            # crashed ("Oops! Something unexpected happened. Refresh"). Qwen was
            # once flagged "login required" in this state and sat idle for 20+
            # minutes, while reloading the home page would have been enough.
            # Login cookies survive the reload.
            if recover and _HOME_URLS.get(self.name):
                log.warning("%s: no input field (%s) — reloading the home page",
                            self.name, val.get("url", ""))
                await self.goto(ws, _HOME_URLS[self.name])
                await self.wait_for_input(ws, timeout=15)
                return await self.check_session(ws, timeout=timeout, recover=False)
            return False, "no_chat_input"
        return True, "ok"

    async def poll_answer(self, ws, script: str, timeout: int = 45,
                          initial_delay: float = 1.0,
                          stable_needed: int = 2,
                          stable_if_generating: Optional[int] = None) -> Optional[str]:
        """Polls the DOM until the answer text stabilises.

        `script` runs on every poll and must return an `{answer, generating}`
        dict (extra keys are ignored).

        SPEED: measurements showed reading the answer takes 81-96% of request
        time, so this loop is sped up in two places:
          1) the poll interval was lowered from 1 second to `POLL_INTERVAL` —
             we notice the end of the answer sooner;
          2) if the site reports "generation finished" (stop button gone) and
             the text has not changed, a single confirmation is enough — it
             used to always wait for two identical polls, adding needless
             time to every request.
        Stability is measured in TIME, so changing the poll interval does not
        reduce reliability.
        """
        await asyncio.sleep(initial_delay)
        prev = ""
        stable_since = 0.0
        js_errors = 0
        deadline = time.time() + timeout

        # Once the answer is finished, unchanged for this long — consider it ready
        need_done = max(0.4, (stable_needed - 1) * cfg.POLL_INTERVAL)
        need_generating = (None if stable_if_generating is None
                           else max(1.0, (stable_if_generating - 1) * cfg.POLL_INTERVAL))

        while time.time() < deadline:
            await asyncio.sleep(cfg.POLL_INTERVAL)
            try:
                val = await cdp.evaluate(ws, script, timeout=20)
            except RuntimeError:
                # The read script may fail briefly while the page re-renders.
                # After several consecutive errors, though, it is a real fault —
                # raise instead of staying silent.
                js_errors += 1
                if js_errors >= 5:
                    raise
                continue
            js_errors = 0
            if not isinstance(val, dict):
                continue
            cur = val.get("answer") or ""
            gen = bool(val.get("generating"))
            if not cur:
                continue

            if cur != prev:
                prev = cur
                stable_since = time.time()
                continue

            if not stable_since:
                stable_since = time.time()
            stable_for = time.time() - stable_since
            need = need_done if not gen else need_generating
            if need is not None and stable_for >= need:
                return self.clean_text(cur)

        return self.clean_text(prev)

    def clean_text(self, text: str) -> str:
        if not text:
            return ""
        # Strip zero-width spaces and other marker characters
        for ch in ("​", "‌", "‍", "﻿"):
            text = text.replace(ch, "")
        return text.strip()
