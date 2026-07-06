# SearXNG Web Research

Query patterns:

- `<term> definition <domain>`
- `<term> Chinese translation`
- `<term> target-language term`
- `<term> official documentation`
- `<term> standard datasheet manual`
- `<term> wiki` only when an encyclopedic result is likely useful

When results are empty:

- Remove extra translated words.
- Try the raw English term plus one domain word.
- Try the target-language term separately.
- Avoid repeating the exact same failed query.

Source ranking:

1. Official standards, documentation, manuals, vendor docs.
2. Maintained reference sites, dictionaries, encyclopedias.
3. Technical articles with enough context.
4. Forum posts and Q&A only when no better source exists.

Fetch policy:

- Do not fetch every result.
- Fetch when the snippet is too short, ambiguous, or lacks target-language evidence.
- Skip search engine internal pages, login/captcha pages, and generic index pages.
