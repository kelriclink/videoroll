# Term Research Policy

Use the video context first. A term should be researched when one of these is true:

- It is a proper noun, product, standard, game item, work-specific phrase, community meme, title, named method, named tool, or specialized domain term.
- The target-language wording has a known conventional translation.
- A wrong translation would confuse the viewer or break consistency across the video.
- The local knowledge base has no confident match.

Skip or keep context-only when one of these is true:

- Single-letter variables in math or logic, such as P, Q, R, x, y.
- Common classroom terms, basic dictionary words, greetings, ordinary actions, measurements, or full sentences.
- Terms that are not actually present in the current subtitle block or previous summary.
- A search result is generic and does not support the current meaning.

Persistence policy:

- Write durable RAG only when evidence supports both the meaning and the translation.
- Use context-only guidance when the translation is useful for this block but not stable enough for long-term reuse.
- Prefer pending over auto-approved when sources are weak, conflicting, or only indirectly support the translation.
