# Verification And RAG Write Rules

Valid evidence should be about the same term and the same sense as the subtitle context.

Good evidence:

- Official documentation, standards, project pages, release notes, manuals, dictionaries, encyclopedias, or well-maintained references.
- Search snippets that contain a clear definition and an unambiguous target-language wording.
- Fetched page text that directly supports the proposed meaning.

Weak evidence:

- Search engine About, Preferences, Search syntax, login, captcha, or category pages.
- Pages that mention the term but describe another sense.
- Forum or social content without enough context.
- English-only sources when the final question is a fixed Chinese translation.

Decision rules:

- If evidence is weak but context makes the meaning clear, provide a context-only card.
- If evidence supports meaning but not translation, do not auto-approve.
- If evidence and subtitle context disagree, do not write durable RAG.
- If sources conflict, prefer official or newer sources and reduce confidence.
