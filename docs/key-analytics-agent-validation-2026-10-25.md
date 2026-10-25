# Per-key analytics and agent streaming — 2026-10-25

## Per-key accounting

User usage now includes key comparison, a daily token/spend chart and breakdowns by public model, project, purpose, client and called tool. The same component provides global/account/key analysis to administrators. Filtering a key also filters existing daily/model charts and request history. Periods are 7/30/90 days using Asia/Tashkent calendar boundaries.

Unused active keys appear with zero usage. Revoked keys retain usage within the selected period. A selected revoked key can still be inspected when its period is empty. Users can only access their own keys; foreign and nonexistent key ids produce the same 404. Administrative analytics require the administrator role. Responses contain no raw key, hash, encrypted secret, prompt or tool arguments.

The analytics queries aggregate in PostgreSQL and use the existing user/time and key/time indexes; no schema migration is needed. Model/project/purpose/client/tool breakdowns show up to 50 groups; the key comparison retains all active or used keys in the selected account scope.

Tool groups are intentionally **non-additive**: each group shows whole-request tokens for requests involving that tool. A request using two tools appears in both groups. Exact per-tool token cost cannot be derived from provider usage and is not invented. Duplicate tool names within one request are counted once. Missing project/purpose labels are retained as unspecified; clients can supply `X-SF-Project` and `X-SF-Purpose` for future requests.

Browser cluster token counts are character estimates. New billing records and portal output now mark them estimated, and analytics also recognize historical rows with known cluster attribution. Historical rows without attribution cannot be reliably reclassified. This changes labelling, not prices or already charged balances.

## Agent reliability and speed

A real two-turn streamed agent round trip through the public gateway succeeded after deployment of the earlier package:

1. ChatGPT returned one `read_file` call; the controlled test client read a real scratch file.
2. The tool result was sent back, and the model returned the correct unpredictable file code with no extra tool call.

No fallback model was used, no stream error occurred and no real workspace file was read. The disposable QA key has zero billing rates and is revoked after validation.

Observed baseline: the required-call turn took **24.109 s**, recovered by one bounded repair. The final-answer turn took **18.465 s**, including **7.199 s** spent asking again after the correct final answer was already available. These are individual observations, not population latency estimates.

Two targeted changes follow that evidence:

- Tool streams use at most **1 second** of preflight instead of the ordinary **12 seconds**. Immediate validation/queue errors can still use HTTP status; later errors remain structured SSE errors. Tool arguments are still sent atomically. Role/keepalive chunks keep the connection observable while generation runs.
- The protocol explicitly distinguishes a serialized client tool request from a `final_answer` envelope. An unambiguous final answer can skip automatic repair **only after the request actually received a tool result**. It cannot bypass `required` or forced calls, and a fresh user task still gets the existing bounded repair policy. Arbitrary prose, malformed envelopes and unsafe YAML tags do not activate the shortcut.
- A short client-execution hint follows the current task, so the model does not interpret a requested client function as a missing browser-native tool. Routing uses the same hint when estimating input size.

The final-answer optimization removes a redundant generation; it does not shorten the model's own generation. Tool streaming preflight changes acknowledgement latency, not the time the tool result becomes available. The retry cap and total request deadline remain in force.

## Validation

- Backend suite: 665 passed, covering ownership, zero-use/revoked keys, admin filters, daily/model consistency, multi-tool overlap, explicit final envelopes and short tool preflight.
- Production frontend build and three-language dictionary checks pass.
- 12 browser scenarios: user/admin × Uzbek/English/Russian × desktop/mobile. Key selection, unused-key zero totals, tool warning and viewport width checks pass.
- A local opencode configuration that still disabled `sable-4.0` tools was backed up and enabled only after the deployed tool/result round trip succeeded. No credentials were copied into the repository.

Live measurements for the new protocol are recorded after the final rollout; baseline results above apply to the preceding deployed revision.

## Post-deployment evidence

The owner authorized production recreation. Both images were rebuilt from the same revision, preserving server-local process/deployment configuration. Source/configuration and PostgreSQL backups, plus immutable rollback image tags, were retained before the first rollout. No browser profiles or provider accounts were edited. All **13/13** browser ports/workers were discovered afterward. Public site returned 200, unauthenticated public models returned 401. Ledger, held-funds and usage invariants passed before/after the first rollout.

Bounded synthetic two-turn streams through the public domain:

| Model/request | First stream bytes | Tool-call turn | Final-answer turn | Repair |
|---|---:|---:|---:|---|
| ChatGPT, before final optimization | ordinary 12s preflight | 24.109 s | 18.465 s | one retry per turn |
| ChatGPT, optimized | 1.724 / 1.853 s | 25.173 s | 8.645 s | tool turn retried once; final did not retry |
| Large-context Gemini request, routed to Qwen | 1.761 / 1.684 s | 8.254 s | 8.800 s | neither turn retried |

Every turn completed with structured SSE and no stream error. The model requested exactly the allowed synthetic path, and the final answer matched the file's unpredictable contents. No fallback header appeared for ChatGPT. Both large-context turns exposed public `input_size` routing headers. The final-answer gain is a **9.820 s difference in these individual observations**, not an aggregate speed guarantee. ChatGPT's first call still required repair, so sustained agent reliability must continue to be monitored.

The production user analytics endpoint returned 200 for account and key filters, seven daily points and estimated-token totals. The same ordinary QA session received 403 for administrator analytics. Local browser checks cover all three languages and both viewport sizes; the production checks use the real public gateway and database.

A final parser hardening accepts fenced/raw JSON and native `function` envelopes as well as YAML. An empty allowlist now admits no tool (including `tool_choice: none`), and invalid JSON/scalar arguments are rejected rather than wrapped into invented arguments. Tests require malformed calls to repair before reaching the client. These changes avoid discarding valid serialized calls and keep unknown tools blocked.
