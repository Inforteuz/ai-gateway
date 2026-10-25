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

## First rollout findings

The first speed revision passed **713 tests** and was deployed after retaining source/configuration and the preceding image. ChatGPT's equivalent short uncached probe changed **10.393 -> 9.348 s**. This is one observation, not an aggregate guarantee. New telemetry measured first text at 2.954 s and about 0.706 s of stabilization; the registry wait is included in the answer phase.

Gemini still took **90.149 s**. This is explicitly **not** reported as a successful speed improvement. Detailed telemetry exposed the reason: the first account consumed 64.637 seconds before another account was dispatched; its 170 DOM polls had **no answer text at all**, while CDP reads consumed only 520 ms. Read-only inspection showed a visible enabled Stop response button, one submitted user-query and zero model-response nodes. The next account eventually succeeded.

This distinguishes a provider accepting a request but never yielding text from a slow polling loop. Shorter DOM polling would not fix the dominant delay.

### Targeted no-text budget

For an **explicit** `none`/`minimal`/`low` effort request, a Stop button without any answer text now gets a bounded first-text window of 30 seconds (`SF_CLUSTER_FIRST_TEXT_TIMEOUT`, 0 disables). This promotes failover sooner instead of consuming the full 60-second per-tab read budget. Unspecified/high effort keeps its existing budget; a growing answer still gets the existing growth grace and total cap. No generation text is truncated by this change.

A restart-readiness connection refusal was observed in the first immediate probe and excluded from model latency measurements. The repeated probe was run after the cluster startup completed. This also confirms that port discovery alone is not HTTP readiness; deployment checks must wait for application startup.

A second uncached ChatGPT probe used the verified pre-opened blank chat and completed in **6.667 s**. `new_chat` was **0.01 s**, compared with **2.24 s** on the preceding cold probe; telemetry confirmed `chat_reused=True`. First answer text arrived at 2.363 s. End-to-end generation varies, so the direct measured mechanism is the 2.23-second reduction in chat preparation rather than attributing the entire 3.726-second baseline difference to code.

The first full rerun caught the existing elapsed-time guard using adjustable wall-clock time: the initial-delay test finished at 0.415 seconds despite a 0.6-second guard. The answer polling loop now uses monotonic time consistently for initial delay, stabilization, growth and first-text deadlines. This preserves elapsed-time guarantees across clock corrections.

After the first-text budget was deployed, another uncached short Gemini probe completed in **41.697 s**: send 10.71 s, answer 26.52 s, first text 25.777 s, stabilization 0.745 s. It succeeded on the first account; **no failover occurred**. Therefore the difference from the earlier 90–92 s probes cannot be attributed to the new timeout. Provider-side variability and send delay remain material.

## Idle browser CPU bottleneck

The server has 20 logical CPUs. Inspection found load averages around 29/23/21 and cluster CPU around 1,459% (about 14.6 logical CPUs). The API and database were mostly idle. Most CPU was in Chrome's software-rendering GPU processes, especially Venice's landing-page background video.

That background is a muted, looping 1920×1080 decorative asset inside an `aria-hidden` wrapper. A reversible live A/B sampled the **same GPU process** over three-second intervals after settling:

| Venice decoration | GPU process CPU (100% = one logical CPU) |
|---|---:|
| Playing | 991.6% |
| Paused | 342.3% |

The observed difference frees about **6.5 logical CPUs**. This is process CPU, not request latency or total server CPU. A later paused sample was 376.3%; concurrent work makes individual samples variable. Temporarily hiding the video/wrapper gave inconsistent additional savings, so no hiding or general animation suppression is included.

The Venice adapter now pauses only the known decorative asset, requiring a hidden wrapper, muted looping playback and no player controls. Videos inside chat turns/assistant content, arbitrary media URLs and videos with controls are left alone. An idempotent document listener catches playback restarting or a replacement element appearing during SPA navigation. Healthy session checks, clean-chat checks and sending reinstall the guard after a document reload. There is no background polling timer or mutation observer. A failed optional media check cannot block a prompt.

Validation: **44 targeted tests**, then **725 full backend tests passed**. The JavaScript behavioral regression ran under Node and checks excluded media, repeated installation, replacement videos and navigation. Live CDP verification confirmed the guard installs, the composer remains present, and an attempted restart of the decoration is paused again. No Chrome startup flags, profiles, account settings or display services were changed.

Capacity recommendation: remove idle rendering load before adding browser instances. Extra tabs on an already CPU-bound host can increase contention; additional independent capacity should follow measured queue wait and provider throughput, not just instance count.

### Rollout and valid-answer checks

The initial media rollout encountered a file-permission error: a restrictive backup umask carried into the Git update, making the changed Python file unreadable by the service user. This temporarily prevented the cluster from starting. Permissions were corrected, the image was rebuilt, and a non-root application import check passed. Deployments must scope the backup umask separately and smoke-test imports as the service user before replacing a running image. After recovery all 13 browser ports were reachable, with 15 workers after discovery.

A Venice smoke request returned `Loading... / Thinking...` in **3.524 s**. **This is an invalid response and is excluded from speed results.** A second observed request confirmed the existing reader was treating the newly rendered `agentic-thinking-indicator` widget as answer text and prewarming before the model finished. The reader now excludes only that measured widget, restores its DOM styles synchronously, and treats its spinner/shimmer as active generation. Text must still stabilize after thinking ends. Regression coverage includes progress-only and partial states, completed text, and restoration after a DOM read error. The updated full suite passed **726 tests**.

Another Gemini probe completed correctly in **59.890 s**, uncached. The first account produced no answer, and the new budget moved to the second account after **30.3 s** of silent reading, instead of the earlier 64.6 s. The first send still needed a second attempt after 10 s. Live inspection confirmed the Send selector matched a visible, enabled button, so an unverified selector replacement or model downgrade is not included. The remaining send/provider delay still prevents a blanket speed claim for Gemini.

## Final deployed state

Both media and reader fixes are deployed in the cluster; the running adapter's SHA-256 matches the committed source (`8bca48e`). The gateway API process did not require replacement for these Venice-only changes. All unrelated server configuration edits were retained. HTTP readiness, all **13 browser ports / 15 workers**, the public site's **200**, and the unauthenticated public API's expected **401** were verified.

The final uncached ChatGPT probe returned the correct arithmetic answer in **8.073 s**, from a cold chat: new_chat 2.23 s, send 1.15 s, answer 4.40 s, first text 2.089 s. The earlier warm-chat probe remains **6.667 s**. These small samples demonstrate functioning requests and the preparation-time saving, not a new aggregate p95.

After restart the decoration guard remained installed and its background video paused. Three later three-second GPU samples measured **217.7%, 417.0%, 461.5%**. A separate total-cluster CPU snapshot was **757.5%**, versus the initial 1,459%; these snapshots are not controlled for identical workload. The same-process playing/paused A/B above is the causal resource-saving measurement.

**Venice inference remains unhealthy upstream.** With the reader corrected, its page displayed `Response failed. Please try again.` after **26.041 s**, and one spaced retry failed again after **26.173 s**. The gateway now returns an upstream error instead of billing/serving a fake successful `Thinking...` answer. HTTP liveness and a present composer do not prove model availability. No further repeated probes or account/model changes were made. Venice should not be relied upon as the only fallback until it can produce valid responses; independent, supported provider capacity is preferable for a latency-critical fallback.

Final validation: **726 backend tests passed**, non-root image import passed before the final restart, and deployed-source parity was checked. No universal latency guarantee is claimed: provider time still dominates the slow Gemini tail.
