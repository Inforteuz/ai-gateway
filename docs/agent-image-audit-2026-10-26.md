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

All **779 backend tests passed**. Targeted regressions cover empty tool results, equivalent browser-open commands, batch duplicates, JSON Schema validation, parallel tool indexes, early stream close, incomplete streams, image completion and opt-in search failures. The shared fake upstream now emits a real finish reason before its DONE marker; premature DONE remains an error in production. Qwen's original 2752×1536 generated file was downloaded and visually checked; the ChatGPT generation and red-square edit were also visually confirmed. The production rollout and post-deployment observations follow below.
