# Browser gateway speed audit — 2026-10-25

Scope: speed only, on top of the completed agent/usage work. No changes to billing, model selection, tool semantics, account concurrency, rate limits, CAPTCHA handling or the direct DeepSeek driver.

## Baseline

The baseline contains 703 passing backend tests. The live cluster runs 15 workers across 13 browser ports. Only two short uncached inference probes were sent initially, spaced at least 30 seconds apart.

The database's preceding 24-hour sample (successful requests, excluding rows explicitly marked as cache hits) shows:

| Public model | Samples | Median | p95 |
|---|---:|---:|---:|
| sable-3.1 | 53 | 8.588 s | 52.419 s |
| sable-3.5 | 209 | 10.074 s | 72.013 s |
| sable-3.7 | 68 | 13.141 s | 95.195 s |
| sable-4.0 | 42 | 9.854 s | 31.448 s |
| sable-1.0 | 43 | 0.736 s | 3.784 s |

These are mixed workloads and historical rows may lack cache attribution. They are useful for detecting the latency tail, not an apples-to-apples model benchmark.

The two new probes:

- Gemini: **92.318 s**, uncached, a two-token arithmetic answer. One account timed out; a second account succeeded. The successful answer reading phase was 3.34 s, while send confirmation and the failed first attempt dominated the total. The aggregated `phases` did not include time spent in the failed read, so summing phases alone understated total latency.
- ChatGPT: **10.393 s**, uncached, a two-token arithmetic answer. Phases: new_chat 2.27 s, page 0.20 s, prepare 0.09 s, send 1.42 s, answer 6.41 s.

Recent Gemini normal requests paid about 2.1 s in new_chat even after prewarm had already left a clean temporary chat. Read-only DOM inspection confirmed two idle Gemini pages were clean temporary chats; other idle pages were regular rather than temporary and must still reset.

## Changes

1. **Check the actual clean page before navigating.** Previously a warm hint older than 600 seconds, or no in-memory hint after discovery, forced navigation even when the DOM was already a blank temporary chat. Now the adapter must strictly confirm the current page is fresh. A conversation is never reused, and image requests still use their separate reset path. Disabling prewarm disables this shortcut. Background prewarm also skips resetting an already clean page.
2. **Strengthen Gemini freshness.** No user-query or model-response nodes may remain, the composer must be visible, no generation stop control may exist, and the existing temporary-mode/logged-out checks must pass. A submitted question with no answer yet is not a clean chat.
3. **Remove ChatGPT's last registry sleep.** The registry still performs all three sidebar inspections and preserves ownership rules, but no longer sleeps after the final unsuccessful inspection. Controlled execution of the old/new methods measured **2.403 -> 1.603 s**, saving **0.800 s** on this branch.
4. **Confirm Gemini submissions promptly.** Confirmation now also recognizes a newly added user-query, using the pre-send count as baseline. It checks immediately and every 100 ms rather than sleeping 500 ms before the first look. The same 10/6-second budgets and fallback send path remain. A previous user-query alone cannot confirm a new send.
5. **Expose missing latency components.** Bounded scalar telemetry records pacing, session preflight, dispatch wait, fresh-chat reuse, first answer text, final stabilization delay, poll count and total CDP read time. No prompt, answer or tool arguments are logged. Failed read timings remain available across bounded attempts. This distinguishes provider time, queueing, DOM overhead and the gateway's own waits.

A ten-run controlled old-blank-chat benchmark with an 80 ms synthetic navigation measured p50 **81.073 -> 0.002 ms**. This proves navigation is skipped; the synthetic 80 ms delay is not a production speed estimate. Actual saved new-chat time is measured after rollout.

## Validation and deployment

Targeted tests cover aged/missing warm hints, dirty chats, disabled prewarm, image isolation, delayed sidebar registration, Gemini user-query confirmation and existing growing-answer/live-stream behavior. Full-suite and post-rollout results follow below.

The Tailscale client was stopped at the start of the audit; it was resumed to restore the existing authorized server connection. The public site was reachable throughout. Existing unrelated local `.claude/` files are excluded from commits.

## Next architectural work

The primary risk is the slow tail rather than a sub-second poll interval. Measure uncached p95 by input size, output size and actual routed provider, then distinguish provider queueing, time to first text and generation duration. Do not shorten long-generation deadlines indiscriminately: that recreates the earlier broken-code/half-tool-call failures.

If a provider is routinely slow after accepting a request, independent authorized provider capacity or native APIs are the scalable remedy. Extra same-account tabs do not remove a provider quota or model inference delay. Speculative duplicate generations would spend quota and create contention; they are not enabled here. Session reuse with transcript binding remains a separate privacy-sensitive architecture task; blank-chat reuse is the only reuse in this change.
