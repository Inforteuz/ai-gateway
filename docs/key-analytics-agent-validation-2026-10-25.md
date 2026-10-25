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

- Backend suite: 659 passed, covering ownership, zero-use/revoked keys, admin filters, daily/model consistency, multi-tool overlap, explicit final envelopes and short tool preflight.
- Production frontend build and three-language dictionary checks pass.
- 12 browser scenarios: user/admin × Uzbek/English/Russian × desktop/mobile. Key selection, unused-key zero totals, tool warning and viewport width checks pass.
- A local opencode configuration that still disabled `sable-4.0` tools was backed up and enabled only after the deployed tool/result round trip succeeded. No credentials were copied into the repository.

Live measurements for the new protocol are recorded after the final rollout; baseline results above apply to the preceding deployed revision.
