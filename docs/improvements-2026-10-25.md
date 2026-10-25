# Routing, cache isolation and agent reliability — 2026-10-25

The public `ai-gateway` repository is the primary implementation for this work. The existing English `/v1/*` error boundary is retained.

## Implemented

- Oversized **text-only cluster** requests can use an enabled, listed model whose adapter input limit fits the composed prompt. The actual adapter `MAX_INPUT_CHARS`, tool capability and shared composer are used; no provider limits are duplicated in the gateway. JSON/reasoning instructions count toward the estimate. Files/images stay on the requested route rather than being fetched twice or losing file support.
- Routing preserves the processing class and caps both input and output prices at the requested rates. Holds and usage remain attached to the requested billing model. Free quotas, per-key limits and monthly budgets remain effective. A cheaper destination can reduce the charge.
- Responses expose `X-SF-Routed-Model` and `X-SF-Route-Reason`. Usage attribution retains the requested/routed public model, reason, estimated composed characters and original input limit. Both users and administrators can inspect the public routed model; infrastructure provider details remain administrative.
- A provider queue timeout can retry once in a different provider pool, after releasing the failed attempt's reservation/leases. The same pool is not retried for capacity exhaustion. `private` requests cannot fail over to third-party processing.
- Cache identity now includes the public catalog model, its relevant reasoning policy and provider-fallback policy, in addition to upstream, prompt, generation parameters and attachments. Client-supplied `sf_*` fields are removed and replaced by gateway-owned values.
- Known cache hits are recorded in attribution, exposed as `X-SF-Cache` for JSON responses and preserved in SSE metadata. Monitoring latency excludes known cache hits. Historical rows without a cache marker remain in the sample; they cannot be reliably reclassified after the fact.
- The tool protocol explicitly explains that the **API client** executes serialized calls in its own environment. The browser model must return YAML, rather than search its native web-chat tools or claim it executed a file operation.
- ChatGPT advertises text-protocol tool calling after a successful live transport probe. This is emulation, not native web-chat tool integration.
- Tool repair remains bounded to one extra request. `required`/forced calls that remain absent return `tool_call_required`, rather than a successful empty agent turn. A forced tool cannot be substituted with another allowed tool. Under `auto`, a legitimate original final answer is preserved if repair adds no call.
- Cluster metrics count tool requests, initial calls, retries, recovered calls, missing calls, retry errors and retry latency by provider, separately from cache hits. Successful request attribution retains bounded retry metadata, never prompt contents or tool arguments.
- The administrator monitoring page displays tool protocol counters and retry p95. Request details show routing/cache/repair status. New copy is available in Uzbek, English and Russian; remaining monitoring category translations and median/Ollama wording were also corrected.

### A correction to the handoff diagnosis

`429 too_many_concurrent` is enforced **per API key**, both by the process counter and `billing.reserve`, not per provider. Bypassing it through fallback would break that limit. Provider admission exhaustion actually returns **503 server_busy** from `_acquire`. The new cross-pool retry applies to this provider-specific failure. Rate limits, daily allowances and billing errors still do not trigger fallback. Fast-path routing uses the provider actually queued, not only the catalog provider.

Also, a private local model with a third-party backup configured will intentionally **not** use that backup. Such processing requires a separately disclosed third-party model; availability does not override the existing privacy promise.

## Measurements

These local controlled checks demonstrate the changed mechanism; they are not end-to-end production speed promises.

| Check | Before | After |
|---|---:|---:|
| Synthetic 32,503-character agent input, composed to 35,326 characters | Gemini requires one context file | Qwen fits directly; zero context files |
| Public models sharing the same upstream and prompt | 1 distinct cache identity for 2 models | 2 distinct identities |
| Provider-capacity test, local verification-enabled model -> cluster backup | No backup attempt on `server_busy` | One successful backup; all holds/slots released |
| Monitoring fixture: one 12,000 ms answer and cache hits at 17/41 ms | Median including cache: 41 ms | Model median: 12,000 ms; 2 cache hits separate |

Thirty warmed local routing decisions: p50 **0.015 ms**, p95 **0.026 ms**. The synthetic input comprises 32,480 instruction characters and a 23-character current task. This avoids confusing raw user size with composed prompt size. Actual file-upload time saved after deployment remains to be measured; the previously reported 4.9 seconds is a historical observation, not a new measured gain.

### Bounded live ChatGPT probes

Three synthetic requests were sent through the running cluster's normal HTTP/balancer path, more than 30 seconds apart. None executed a filesystem tool; the requested path was synthetic.

| Probe | Cache | Time | Parsed `read_file` calls |
|---|---|---:|---:|
| Existing protocol | Miss | 16.34 s | 0 |
| Existing retry wording | Miss | 70.91 s | 0 |
| Explicit client-execution transport | Miss | 8.25 s | 1 |

The third answer encoded the expected path correctly. This verifies the transport hypothesis on one provider response, **not** sustained reliability or a full opencode turn. Full tool-enabled endpoint validation must follow deployment, using a synthetic workspace file and a tightly limited number of probes.

Pre-change cluster logs were inspected: the available window contained no `tools: no action block` retry events. It therefore cannot establish a recovery success rate. The new counters and durable attribution provide that evidence going forward.

## Validation

- Baseline: **626 backend tests passed**.
- Updated final suite: **645 backend tests passed**, including the final provider-selection and image-capability corrections.
- Production-shaped tests use real isolated PostgreSQL schemas and restricted application permissions, including reservation/lease cleanup, JSON/SSE routing, price ceilings, per-key limits, private-data isolation and client namespace spoofing.
- Frontend production build and both dictionary consistency checks pass.
- Browser QA: **12 combinations** (2 affected pages × 3 languages × desktop/mobile), no page exceptions or viewport overflow. Ordinary users receive **403** for administrative cluster metrics.
- Browser tests use a local isolated database and synthetic cluster metrics, not production account data.

## Deployment boundary and next checks

No production file, account, environment setting or container was changed/restarted for this work. Existing browser profiles, direct DeepSeek driver, relay, VNC and Xvfb were untouched. Three authorized synthetic inference probes are the only live model calls.

The API image now includes the cluster Python source so catalog capabilities, adapter limits and composition can be imported without launching browser processes. Build **both API and cluster images from the same revision**; no schema migration is added (schema stays v9). If cluster prompt-shaping environment overrides differ from API defaults, provide the same `SF_CLUSTER_*` composition settings to the API before relying on the character estimate. Attachment/overflow handling remains the final safety net.

Deployment sequence after owner approval:

1. Snapshot current source/configuration and retain immutable old API/cluster image tags. Preserve server-local deployment changes; do not overwrite its working tree.
2. Rebuild the API and cluster from the reviewed revision, including the API Dockerfile change.
3. Recreate the required services during an approved window. Cluster recreation disconnects all open VNC sessions; profiles remain in their volume.
4. Confirm every configured browser port/worker and public domain status, not only the internal gateway port.
5. Send one real tool-enabled ChatGPT request through `/v1/chat/completions`, then a synthetic-file agent turn. Only then change the corresponding opencode `sable-4.0` `tool_call` setting to true. It remains unchanged until the deployed endpoint supports tools.
6. Observe input-size routes and provider-capacity fallbacks. Confirm actual public model, price/ledger invariants, key limits and JSON/SSE attribution.
7. Compare uncached latency and upload phases before/after on equivalent inputs. Avoid dense benchmark traffic; wait at least 30 seconds between browser-provider probes.

## Capacity recommendations

Measure active/queued accounts and saturation before adding more instances or increasing the cluster concurrency setting. Spare accounts should be independent. Tabs sharing one account do not add independent quota or account capacity.

Avoid making every fallback path depend on one provider, even when it has multiple accounts. Add an authorized independent provider/API fallback for models whose disclosed processing class permits it. Do not route private traffic there implicitly. A second physical host helps with browser-host failure; another tab on the same host does not.

Session-bound chat reuse remains a separate design: it needs API-key/tenant binding, a server-authenticated session identifier, transcript-prefix validation, TTL/eviction, exclusive ownership and reset on ambiguity. Hashing arbitrary client history alone is not sufficient session authorization. Fresh-chat isolation is retained.

The old GPU relay remains an infrastructure dependency. A direct authenticated tunnel to the GPU host would remove that hop once its owner provides access; that host was not accessible within this task.

CAPTCHA/slider challenges are detected and quarantined with failover/cooldown; they are not automatically solved or bypassed. Human verification may still be required by the provider.
