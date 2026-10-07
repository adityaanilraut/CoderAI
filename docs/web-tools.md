# Web tools

`WebSearch` and `WebFetch` return external source material. Pages, snippets,
metadata, and provider summaries are untrusted data; embedded instructions must
never control the agent. Known prompt delimiters are defanged, and tool results
include an explicit trust notice and `untrusted` metadata.

## Search

Provide either `query` or `queries` (one to four non-empty strings, at most 2,000
characters each). Optional arguments:

| Argument | Behavior |
| --- | --- |
| `provider` | `http`, `exa`, `perplexity`, `deepseek`, `custom`, or a registered provider |
| `max_results` | 1–50 sources, default 8; output budgets may reduce this |
| `use_cache` | Default `true`; `false` bypasses the ten-minute search cache |
| `include_domains`, `exclude_domains` | Up to 20 hostnames each, including their subdomains; optional `*.` prefix |
| `start_date`, `end_date` | ISO publication dates or datetimes; Exa or Perplexity required |
| `recency_days` | 1–3650 days, mutually exclusive with `start_date` |
| `language` | Two-letter language code, such as `en`; Perplexity required |
| `region` | Two-letter country code, such as `US`, used to bias results |

Exa supports domain/date/location filters and returns highlights. Perplexity
uses its Search API for filtered requests (up to 20 results, date precision of
one day) and Sonar with citations for unfiltered requests. DeepSeek uses the
native `web_search` tool through its Anthropic-compatible Messages endpoint.
DeepSeek and custom scripts explicitly reject structured filters. The free HTTP
provider supports domain constraints and country bias via Bing, but cannot
enforce date or language filters. HTML search services can change their markup
or block automated requests; such failures return an actionable error.

Domain constraints are also enforced on returned citations. Synthesized
summaries are omitted when domains are constrained, since summaries may cite
sources outside the returned list. Provider errors can fall back to free HTTP
search; security denials never trigger fallback. One failed query does not
discard successful queries. If all queries fail, the tool returns `ok=false`.
The terminal shows errors, fallback warnings, and summaries.

Search text is limited to 30,000 characters. URLs, snippets, titles, and metadata
are bounded and deduplicated. `truncated`, `fetchedAt`, `fromCache`, and
`cacheAgeSeconds` explain completeness and freshness. Cache entries are isolated
by provider configuration, credentials, filters, network policy, and output budget.

## Configuration

Credentials are resolved for each call, including keys configured after startup:
`EXA_API_KEY`, `PERPLEXITY_API_KEY`, and `DEEPSEEK_API_KEY`. They can also be stored
in the `env` block of user settings or trusted project settings.

Select a default with `CODERAI_WEB_SEARCH_PROVIDER` or `webSearchProvider` in
settings. Without an explicit choice, a configured custom script takes priority,
then available paid providers, then free HTTP search. Language-filtered requests
prefer Perplexity. An invalid explicit provider produces a configuration error.

Configure a script with `CODERAI_WEB_SEARCH_TOOL` or `webSearchTool`. It receives
the query as one argument and runs with a scrubbed environment. Plain stdout is
accepted as a summary; JSON can provide `content` and a `sources` array whose
entries have `title`, `url`, and optional `snippet`/`date`. Custom scripts are
trusted executables and manage their own network access; the built-in HTTP
policy does not intercept their subprocess traffic.

## Fetch

`WebFetch` supports HTML, plain text, JSON, XML, and text-based PDFs. HTML
extraction preserves headings, links, lists, tables, and code examples, resolves
relative URLs, and omits scripts and hidden elements. Unsupported binary content,
empty pages, and password-protected PDFs return explicit errors. Scanned PDFs
require OCR; JavaScript-only and authenticated pages require another retrieval
method. Fetch does not run JavaScript or provide browser sessions.

Use `max_length` (1–100,000, default 30,000) to bound the complete returned text.
When `truncated` is true, repeat the request with `offset` set to `nextOffset`.
`section` selects a Markdown heading; PDF page headings use `Page 1`, `Page 2`,
and so on. Offsets refer to extracted text within the selected section. Fetch
responses cache for five minutes; keep caching enabled for stable continuation,
or set `use_cache=false` for a fresh response. Metadata includes the actual HTTP
status, final URL, content type, character count, and cache age.

Downloads are streamed with a 5 MiB limit on decoded response bytes. PDF
extraction is limited to 200 pages, 10 MiB decoded page streams, and two million
text characters. Fetch and search have 60-second tool timeouts.

## Network policy

Every built-in provider and fetch request uses the shared HTTP transport. It
checks the initial URL, cached destination, and every redirect against SSRF and
domain policies. Anonymous public GET redirects are supported, credentials are
stripped on origin changes, HTTPS downgrade redirects are rejected, and POST
requests cannot redirect to a different origin.

User settings and trusted project settings can contain:

```json
{
  "network": {
    "allowedDomains": ["*.python.org", "api.exa.ai"],
    "blockedDomains": ["blocked.example"],
    "allowPrivateIps": false,
    "enforceSsrfProtection": true
  }
}
```

An allowlist must include the selected provider endpoints and any redirected
destinations. Untrusted projects cannot override network policy or custom search
executables. Private and loopback IPs are blocked by default.

Regression tests: `tests/test_web.py`, `tests/test_web_hardening.py`, and
`tests/test_security.py`, run independently using the project virtualenv.
