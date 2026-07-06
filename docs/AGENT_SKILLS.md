# Agent Skills

VideoRoll can load Agent Skills for subtitle RAG research. A skill is a runnable guidance package: it gives a child Agent extra instructions, optional resources, and optional tool constraints. The skill itself does not execute arbitrary scripts. Actions still go through registered tools such as `rag_lookup`, `wiki_search`, `search_web`, `fetch_url`, and `finish`.

## Locations

- Built-in skills: `src/videoroll/apps/subtitle_service/skills/`
- User skills: `data/agent_skills/`
- Override user skill directory with `VIDEOROLL_AGENT_SKILLS_DIR`.

Each skill lives in its own directory and contains either `skill.json` or `SKILL.md`.

## skill.json

```json
{
  "name": "my-hardware-skill",
  "description": "Research hardware terms with official sources first.",
  "domain": ["hardware", "electronics"],
  "triggers": ["VGA", "HDMI", "pinout"],
  "allowed_tools": ["search_web", "fetch_url"],
  "runnable": true,
  "run_mode": "agent_guidance",
  "instructions": "Use official manuals and standards before blogs. Fetch a page only when snippets are insufficient.",
  "resources": [
    {
      "name": "source-rules",
      "path": "source-rules.md",
      "description": "Preferred source order."
    }
  ]
}
```

`allowed_tools` is optional. Leave it empty to allow all currently enabled tools. If it is set, the child Agent sees only those tools plus `rag_lookup` and `finish`.

## SKILL.md

```markdown
---
name: chemistry-terms
description: Research chemistry subtitles.
domain:
  - chemistry
triggers:
  - reflux
  - condenser
allowed_tools:
  - search_web
  - fetch_url
---
# Chemistry Terms

Prefer IUPAC, SDS, textbook, or university sources. Keep lab procedure wording consistent with the subtitle context.
```

## Selection

For each discovered term, VideoRoll matches skills by `domain`, `triggers`, and current subtitle context. Selected skills are injected into the child Agent prompt and shown in the Agent trace as `skill_activated`.
