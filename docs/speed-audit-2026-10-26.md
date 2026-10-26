# Conversation latency and bounded browser recovery — 2026-10-26

Scope: continue the measured speed work; include stale timeout status and the Gemini response retry control reported by the operator. No provider/model downgrade or cross-user conversation reuse.

## Measurements before changes

The preceding 24-hour successful uncached sample contained 45 `sable-3.5` requests (median 7.846 s, p95 102.155 s) and six `sable-3.7` requests (median 15.618 s). These are mixed workloads, not controlled benchmarks.

Recent Gemini requests used a prewarmed clean page in 0–0.01 s. Failed accounts still consumed about 61–63 s before moving on, producing totals of 81–115 s. A configured 30-second no-text guard existed, but only inspected the client's explicit effort. When the client omitted effort and the adapter selected its `low` default, polling did not see that effective level.

Two spaced, uncached Qwen fact-recall probes returned the correct reference number:

| Composed request | New chat | Send | Answer | Total |
|---|---:|---:|---:|---:|
| 12,096 chars | 0.00 s | 0.85 s | 6.67 s | 7.767 s |
| 52,694 chars | 0.00 s | 1.79 s | 10.93 s | 13.033 s |

Both used fresh blank chats. The longer full-history input increased send and first-answer latency (first text 5.602 vs 9.868 s). Temporary-chat setup was not the dominant measured cost.

On one Gemini account, a disposable temporary-chat probe and a separate blank regular-chat probe both produced no answer text within 30 seconds. The regular attempt had a submitted user-query, no model-response, and a Stop control after 30.326 s. This does not establish all-account performance, but provides no evidence that disabling temporary mode cures the observed stall. Test tabs were closed afterwards; the operator's conversation was not changed.

## Changes

1. **Do not duplicate the current task after tool results.** The composer previously included the last user task in history and again in the current-task section. It now moves that exact task to the current section, preserving system instructions, tool calls/results and the existing context budgets. A controlled comparison with a 9.5k-character task changed **22,376 → 12,834 chars**, saving **9,542 chars / 42.6%** for that fixture. This is a composition-size result, not a universal latency reduction. Prior context is not summarized or discarded by this change.
2. **Use the effective reasoning effort for the no-text guard.** Polling now sees the adapter's selected default as well as an explicit request. An effective fast level gets the existing 30-second first-text budget. High effort and growing answers retain their existing behavior.
3. **Retry a technical failure in place once.** Empty output, an exhausted first-text wait or a short recognized provider error may use the provider's existing regenerate control if nothing has streamed and at least two seconds remain in the original read budget. The second read is capped at 12 seconds and the existing request deadline still applies. It does not resend the full conversation. A content-policy refusal, quota/login/rate-limit response or already committed stream does not trigger this repair.
4. **Recognize Gemini's actual Redo menu.** Live DOM inspection confirmed the circular-arrow control is labelled `Redo`; the old generic retry matcher did not recognize it. A click opens a menu containing Longer / Shorter / Try again (Russian: Длиннее / Короче / Повторить). The adapter now targets only the latest response's visible enabled retry control, selects the exact retry entry after the menu animates, and confirms generation started before reading again. It does not click unrelated refresh icons or verification controls. An observed active Stop control prevents another regeneration.
5. **Clear a stale timeout after a successful admin reload.** A plain healthy DOM probe does not prove inference recovered. Only an explicit reload followed by a healthy probe clears a stale timeout/empty/send error and its transient cooldown. Account-level quota/rate-limit blocks are preserved. This fixes cards that showed a green healthy session and an old red timeout after a successful reload.
6. **Cover the reported small-talk variants.** `Hey nima gap`, `Nima gap`, `How are u` and `Salom qalaysan` now qualify for the existing short-message fast path. Full matches, text-only input and existing forced-tool/JSON protections remain. Real tasks and continuation commands retain their original path.

## Validation

Targeted recovery tests cover default low effort, explicit high behavior (existing suite), the retry budget, one retry only, already streamed text, content-policy refusals, latest-response Redo selection, stale status after reload, account blocks from sibling tabs, and preservation of the exact task and tool result.

The updated Redo code also ran in an isolated real Chrome tab against a fixture matching the measured two-stage menu, including a 250 ms menu delay. It started **exactly one** regeneration in **0.341 s**. This is a real browser-control check, not a successful upstream Gemini generation. A separate real Gemini test submitted the exact test prompt but still produced no text within 30 s, so no provider-speed success is claimed.

## Findings during rollout

A real tool-result probe returned the correct fact in **31.088 s**, but telemetry revealed **two generations**: the low-effort request had received the gateway's additional YAML plan/check/answer template, then an invalid template caused a complete re-ask. Explicit `none`/`minimal`/`low` requests now skip that optional template. Medium/high/default behavior and explicit JSON formats retain their existing rules. The gateway's input-size estimator uses the same decision.

The equivalent uncached probe after this correction returned the correct fact in **20.469 s** with **one generation**, `reasoning=False`, first text **3.839 s**, send **15.33 s**, answer **4.90 s**. Earlier phases were send 16.91 s and answer 12.87 s across the two generations. These are two variable provider observations, not a p95 estimate. The removed second generation is the verified mechanism; the remaining send delay is unresolved.

Ordinary live traffic exposed a **165.3-second** Gemini request that tried five accounts, four with no text. A previously configured, capability-compatible stand-in may now take over after **two distinct failed accounts** (`SF_CLUSTER_SPILL_AFTER_FAILURES`, default 2, 0 disables). The catalog controls the destination for both streaming and plain requests; clients cannot choose it. An unavailable or capability-incompatible stand-in is skipped. Without a configured stand-in, the original provider traversal remains. Cross-provider fallback is marked after execution so provider-variant metadata cannot accidentally overwrite it.

The reload result now also reports failure when navigation fails even if the old page still has a healthy composer. A stale error is cleared only after navigation, input readiness and the session check succeeded. A failed reload cannot claim success merely because its old session remained visible.

## Final validation and rollout

- **745 backend tests passed** on the expanded changes; the final gateway fallback forwarding change passed its 29 targeted routing/recovery tests, and the final retry-budget changes passed 96 targeted tests. The final reload-result clarification passed all 32 instance tests.
- The request composer regression confirms a large user task appears once and tool-result content/identifiers remain present. Existing tool protocol, long-answer growth, stream integrity, capability routing and accounting tests pass.
- New images were built on top of the production configuration, with backups and preceding images retained. Unrelated local server modifications were preserved. Application imports as the service user passed before replacement.
- The deployed Redo/menu implementation was verified in an isolated Chrome fixture and started exactly one retry. No claim is made that an upstream Gemini failure was successfully regenerated: actual probes sometimes had no response/control at all. Such a tab moves on instead of clicking a generic page refresh.
- The new `Hey nima gap` compact fast-path payload produced **“Salom! Qanday yordam bera olaman?” in 0.806 s** on the live fast model, using **796 input characters**. This measured the generated compact payload directly against the configured upstream; offline gateway integration tests cover selection and guards. It is not a public end-to-end timing.
- The admin reload endpoint returned success and an `ok` card with no error in a live idle-instance check. That instance had no stale error initially, so stale-error removal itself is established by the endpoint regression tests.
- Public site **200** and unauthenticated API **401** were verified after rollout. Browser discovery reached **13 ports / 15 workers**. Read-only billing checks reported all seven invariants holding.

Remaining limitations: long conversations are still fully replayed to a fresh isolated chat; provider prefill/generation and occasional slow send confirmation remain. The implementation removes a duplicate task, rather than silently summarizing or discarding context. A persistent provider conversation would require authenticated session binding, exact-prefix matching, exclusive worker ownership, cleanup and bounded storage before it can safely replace this isolation. Disabling temporary mode alone did not solve the measured stalls. No more browser instances were added to the existing host.
