# Remote Plugin Indirect URL Design

## Summary

Extend remote spider plugin loading so the configured URL may return either plugin source code directly or a single indirection URL. If the first response body, after trimming leading and trailing whitespace, is exactly one `http://` or `https://` URL, the loader should fetch that URL once and treat the second response body as the real plugin source. Otherwise the first response body remains the plugin source.

This keeps the existing plugin manager UI and persistence model unchanged while allowing remote plugin distribution endpoints that publish a "real source URL" instead of Python code.

## Goals

- Keep `添加远程插件` using the existing single URL input.
- Support remote plugin URLs whose body is exactly one `http(s)` URL.
- Limit indirection to one extra fetch.
- Reuse the same behavior for initial add, manual refresh, and normal remote load resolution.
- Avoid misclassifying ordinary Python source code as an indirection URL.

## Non-Goals

- Supporting multi-hop URL chains.
- Parsing JSON, HTML, or custom manifest formats to find a source URL.
- Changing the stored plugin `source_value`; the original configured URL stays authoritative.
- Adding new plugin manager controls or source types.

## Scope

Primary implementation lives in:

- `src/atv_player/plugins/loader.py`

Primary verification lives in:

- `tests/test_spider_plugin_loader.py`

## Design

### Indirection Rule

Remote plugin loading keeps the current fetch flow, with one additional decision after downloading the configured remote URL:

1. Fetch the configured remote plugin URL with the existing timeout and redirect policy.
2. Trim the response body with `strip()`.
3. If the trimmed body is exactly one `http://` or `https://` URL, fetch that URL once.
4. Use the second response body as the final plugin source text.
5. If the trimmed first body is not a single URL, use the original first response body as the final plugin source text.

The loader must not attempt any further URL expansion after the second fetch. The second response body is always treated as plugin source, even if it also looks like a URL.

### URL Detection Boundary

The URL detector should stay intentionally narrow:

- Accept only trimmed bodies that start with `http://` or `https://`.
- Require the entire trimmed body to equal that one URL.
- Reject bodies with extra lines, prefixes, suffixes, comments, JSON wrappers, or HTML.

Examples:

- `"https://cdn.example.com/spider.py"`: indirect URL, perform one extra fetch.
- `"\nhttps://cdn.example.com/spider.py\n"`: indirect URL after trimming.
- `"print('hello')"`: plugin source, no extra fetch.
- `"url=https://cdn.example.com/spider.py"`: plugin source, no extra fetch.
- `"https://a\nhttps://b"`: plugin source, no extra fetch.

This boundary prevents accidental interpretation of valid Python code, comments, or arbitrary text payloads as source indirection.

### Cache Behavior

Cache semantics stay the same:

- After the final source text is resolved, write that text into the plugin cache file.
- `cached_file_path` continues to point to the local cached Python file.
- If either the first fetch or the optional second fetch fails, keep the existing fallback behavior of reusing a non-empty cached file when available.
- If no usable cache exists, propagate the error so the manager can persist the failure state and log entry.

This means the cache always stores executable plugin source, never the intermediate URL text.

### Error Handling

No new UI error type is required. Failures should surface through the existing remote loader path:

- network failure on the first fetch: handled exactly as today
- network failure on the second fetch: handled by the same cache fallback and error recording path
- HTTP status error on either fetch: handled by the same cache fallback and error recording path

The plugin manager continues showing the last error text and keeping the previous cached plugin when refresh cannot complete successfully.

### Testing

Add focused loader tests for:

- successful one-hop resolution where URL `A` returns URL `B`, and `B` returns plugin source
- non-indirect bodies that look like ordinary Python source and must not trigger a second fetch
- refresh failure on the second fetch still falling back to an existing non-empty cached plugin

Existing direct remote plugin tests should continue to pass unchanged.

## Open Questions

None. The design intentionally fixes the indirection depth at one hop and fixes detection to "trimmed body equals one `http(s)` URL".
