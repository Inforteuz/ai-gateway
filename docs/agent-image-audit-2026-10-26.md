# Agent reliability, web grounding and image providers — 2026-10-26

## Reported failures and fixes

The operator's browser-open task returned empty successful shell results, then repeated the same `open` command across browsers. Empty `role:tool` messages were being dropped before composition and size estimation. They are now preserved with an explicit no-output completion note. Within the latest user task, a completed simple macOS `open` target cannot be emitted again, including a different browser or an equivalent home/file URL. An explicit tool failure permits retry; a new user turn permits reopening. Identical opens within one batch are deduplicated. This is a narrow guard for simple commands, not a general guarantee against duplicate effects in arbitrary scripts or separate client retries. No operator browser tabs were opened by these tests.

Tool calls are validated against the caller's complete JSON Schema, including nested required fields and local references. Invalid types, unknown tools, malformed siblings, excessive batches and non-JSON YAML values are rejected atomically, before any call reaches the client. External schema references are never fetched. One existing bounded repair path remains. The prompt includes the compact schema once. Parallel streamed calls now carry separate indexes. An explicit final-answer envelope under automatic tool choice can end a direct question without an unnecessary extra generation; promises to act still need repair. Contextual identity follow-ups also qualify for the existing compact fast path.

A streamed answer that ends without choice completion is reported as `stream_incomplete`; it cannot silently appear finished. Complete choices missing only the terminal marker receive that marker. Closing the cluster stream cancels its unfinished inference task and releases its resources, including when closed immediately after the assistant-role chunk. Existing partial-output billing is preserved; a failed stream with no actual model output remains free.

## Image providers and measurements

These are individual uncached observations on the same host, not p50/p95 estimates or a guaranteed improvement:

| Provider / operation | Before deployment | Result |
| --- | ---: | --- |
| ChatGPT generation | 24.832 s | One image, about 1.03 MB |
| ChatGPT edit | 24.123 s | One edited image |
| Gemini generation | 50.466 s | One image |
| Qwen Create Image | 56.386 s after send | Real generated image |

Gemini image generation was already enabled and is usable. Qwen's normal Fast chat only answered in text; the separate **Create Image / Qwen-Image 3.0** mode works and is now selected explicitly for image generation. Its skeleton/loading icon and gallery examples are excluded. The result comes from the latest assistant answer, with the thumbnail resize parameter removed to download the original asset. Image mode is not reused for normal text requests. Long image prompts retain their requested provider rather than changing image models based on text-field capacity.

Qwen accepted an edit attachment but produced no final image in a 90-second observation. Editing is therefore advertised separately from generation and stays disabled for Qwen until verified; ChatGPT and Gemini retain their measured edit support. Adding a provider does not establish that it is faster.

Image completion polling uses a monotonic clock and a 0.5-second interval. Providers with reliable completion controls must finish before a stable-looking preview is returned. Loaded natural dimensions are checked; uploaded inputs are excluded. This removes avoidable polling delay and prevents unfinished images from being returned; it does not shorten the provider's generation itself or reduce image quality.

## Optional internet grounding

`docker-compose.search.yml` adds a private pinned SearXNG service (384 MB / one CPU limit, no published port). Generate `SF_SEARCH_SECRET`, add the file to `COMPOSE_FILE`, and start with `docker compose --profile search up -d search cluster`. Keep the same compose files on later deployments. The image's secret environment override was verified with the read-only mounted settings.

Clients opt in on `/v1/chat/completions`:

```json
{
  "model": "YOUR_PUBLIC_BROWSER_MODEL",
  "messages": [{"role": "user", "content": "Find the official Python TaskGroup documentation and cite it."}],
  "web_search": {"query": "site:docs.python.org asyncio TaskGroup", "max_results": 3}
}
```

Only the explicit query is sent to search engines, never the full conversation. Results are bounded untrusted snippets with source URLs in `gateway.sources`; this code does not fetch linked pages. Query length is 1–500 characters, result count 1–5, response limit 256 KB, timeout 10 seconds. Private/executable result links and redirects are rejected. Invalid options return a client error; an unavailable backend returns an explicit error without inventing sources. Search cannot be combined with image generation, forced tools or strict JSON output. Local models cannot silently ignore it. Existing client-provided search tools continue to execute in the client.

The private service returned 25 results in 1.344 seconds in an initial real query. Search latency and availability depend on its upstream engines. It is opt-in; no full-conversation automatic search is enabled.

## Validation and rollout

All **782 backend tests passed** on the final code, including image setup serialization and the compact identity-context adjustment. Targeted regressions cover empty tool results, equivalent browser-open commands, batch duplicates, JSON Schema validation, parallel tool indexes, early stream close, incomplete streams, image completion and opt-in search failures. The shared fake upstream now emits a real finish reason before its DONE marker; premature DONE remains an error in production. Qwen's original 2752×1536 generated file was downloaded and visually checked; the ChatGPT generation and red-square edit were also visually confirmed. The production rollout and post-deployment observations follow below.


## Production observations

- Code and all three interface languages were built and deployed. The production-specific dependencies, Gunicorn setup and unrelated server files were preserved. Search runs persistently on the private compose network; temporary search containers were removed. Previous application images and protected local configuration backups were retained.
- A real streamed two-file tool request produced indexes **0 and 1**, intact JSON arguments and `finish_reason: tool_calls`, in **26.859 s**. It needed one repair (6.343 s); the aggregate send phase was 16.18 s. This verifies delivery and also shows remaining latency, not a universal speed improvement.
- The browser-open history probe included a completed empty tool result. It ended in **11.883 s**, with **zero new calls**. No browser-open command was executed by the probe. Unit regressions separately establish deterministic suppression when the model actually repeats the call.
- Opt-in search plus a brief Qwen answer took **6.762 s**, including **1.115 s** search, returned three official Python documentation source links, and cited the official URL.
- A later ChatGPT generation took **86.067 s**; 83.14 s was image waiting. An isolated edit took **26.149 s after send**, plus **11.37 s preparation**. Its first loaded result appeared at **25.985 s**, when generation was already complete, and the red-square edit was visually verified. These observations do **not** establish an image-generation speedup; upstream time remains variable.
- The first Qwen image API integration attempt exposed an old-composer navigation race: a failed image-mode reset was swallowed and the request waited in the wrong mode. Image reset errors now propagate to dispatch before sending. Qwen waits for a newly loaded document and checks its image mode again before choosing a model; a normal text model cannot silently handle an image task. Two live resets from a populated test chat confirmed a blank image composer in **8.208 s** and **8.424 s**. The additional full-endpoint result is recorded below.
- Model cards and examples now distinguish image generation from image editing in Uzbek, Russian and English. Qwen generation is listed; unverified Qwen editing is excluded. The frontend production build passed.

The old-composer fix and the image-mode failure and concurrency regressions are included in the 782-test run. No more Chrome instances were added. Long agent histories still require full replay within the existing context budgets; these changes do not provide persistent user-bound provider conversations or exactly-once execution across independent client requests.


### Foreground menu failure reproduced and fixed

A subsequent Qwen image API call did return a real original image in **139.264 s**, including two failed background mode setups of about 30 seconds each; the successful attempt spent **9.37 s** opening the image composer, **65.95 s** waiting for generation and **2.35 s** downloading. The orange-triangle output was visually verified. This exposed a second issue beyond navigation: Qwen's image menu ignores clicks when its tab is hidden.

A controlled real-browser comparison reproduced the failure on an initially hidden tab in **29.588 s**; the same tab, brought forward, selected image mode in **8.291 s**. The adapter now brings its controlled image tab to the foreground before navigation. Two further tests that deliberately hid it first succeeded in **9.056 s** and **5.497 s**. Image setup is serialized within each account so parallel requests cannot steal focus during menu selection; generation after setup can still overlap. No additional model generations were used for these menu checks.


The final production endpoint probe after the foreground fix completed successfully: Qwen returned one image in **83.951 s**. Its phases were **9.44 s** new image composer, **1.03 s** send, **71.23 s** provider generation, and **2.04 s** download. This confirms the fixed path reaches Qwen Image 3.0 without the earlier 30-second background-menu retries. Provider generation remains the dominant variable cost.
