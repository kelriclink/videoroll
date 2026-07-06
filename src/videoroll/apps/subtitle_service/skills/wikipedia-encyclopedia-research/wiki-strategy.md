# Wikipedia Research Strategy

Wikipedia is useful for:

- Named scientific objects or phenomena.
- People, places, works, organizations, fictional settings, and games.
- Background explanations where exact wording is less important than correct sense.

Wikipedia is not enough when:

- The translation requires a conventional Chinese term not present in the evidence.
- The page is about a broad category but the subtitle uses a narrower technical sense.
- The search result points to a disambiguation, unrelated, or low-detail page.

Tool sequence guidance:

1. Try wiki_search for encyclopedic topics.
2. If the result is relevant but lacks target-language wording, use search_web.
3. If search snippets are too short, fetch_url for the best non-internal result.
4. Finish only after evidence supports the meaning, or explicitly mark the result as not writable.
