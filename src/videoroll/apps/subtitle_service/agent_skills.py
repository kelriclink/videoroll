from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from pydantic import BaseModel, Field


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_HEADING_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
_LIST_ITEM_RE = re.compile(r"^\s*-\s+(.+?)\s*$")
_DEFAULT_USER_SKILLS_DIR = Path("data/agent_skills")


class AgentSkillResource(BaseModel):
    name: str
    path: str = ""
    description: str = ""
    content: str = ""


class AgentSkill(BaseModel):
    name: str
    description: str = ""
    domain: list[str] = Field(default_factory=list)
    triggers: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    instructions: str = ""
    resources: list[AgentSkillResource] = Field(default_factory=list)
    runnable: bool = True
    run_mode: str = "agent_guidance"
    source: str = "user"
    path: str = ""

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "domain": self.domain,
            "triggers": self.triggers[:20],
            "allowed_tools": self.allowed_tools,
            "runnable": self.runnable,
            "run_mode": self.run_mode,
            "source": self.source,
            "path": self.path,
            "resource_count": len(self.resources),
        }

    def prompt_payload(self, *, max_instruction_chars: int = 3200, max_resource_chars: int = 1200) -> dict[str, Any]:
        return {
            **self.summary(),
            "instructions": self.instructions[:max_instruction_chars],
            "resources": [
                {
                    "name": item.name,
                    "path": item.path,
                    "description": item.description,
                    "content": item.content[:max_resource_chars],
                }
                for item in self.resources[:5]
            ],
        }


def default_builtin_skills_dir() -> Path:
    return Path(__file__).with_name("skills")


def default_user_skills_dir() -> Path:
    value = os.getenv("VIDEOROLL_AGENT_SKILLS_DIR", "").strip()
    return Path(value) if value else _DEFAULT_USER_SKILLS_DIR


def _as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace("\n", ",").split(",")
    elif isinstance(value, Iterable):
        raw = list(value)
    else:
        raw = [value]
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        text = " ".join(str(item or "").strip().split())
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text[:120])
    return out


def _safe_name(value: Any, *, fallback: str) -> str:
    text = " ".join(str(value or "").strip().split())
    if not text:
        text = fallback
    return text[:120]


def _contains_trigger(haystack: str, trigger: str) -> bool:
    clean = trigger.strip().lower()
    if not clean:
        return False
    if len(clean) <= 2:
        return re.search(rf"(?<![a-z0-9]){re.escape(clean)}(?![a-z0-9])", haystack) is not None
    return clean in haystack


def _parse_frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER_RE.match(markdown)
    if not match:
        return {}, markdown
    meta: dict[str, Any] = {}
    current_key = ""
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = _LIST_ITEM_RE.match(line)
        if item and current_key:
            meta.setdefault(current_key, [])
            if isinstance(meta[current_key], list):
                meta[current_key].append(item.group(1).strip())
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key.strip()
        clean_value = value.strip()
        if clean_value:
            meta[current_key] = clean_value
        else:
            meta[current_key] = []
    return meta, markdown[match.end() :]


def _resource_from_spec(root: Path, spec: Any, *, max_chars: int) -> AgentSkillResource | None:
    if isinstance(spec, str):
        name = Path(spec).name
        rel_path = spec
        description = ""
    elif isinstance(spec, dict):
        rel_path = str(spec.get("path") or "").strip()
        name = _safe_name(spec.get("name") or Path(rel_path).name, fallback=Path(rel_path).name or "resource")
        description = str(spec.get("description") or "").strip()[:300]
    else:
        return None
    if not rel_path:
        return None
    try:
        root_resolved = root.resolve()
        path = (root / rel_path).resolve()
        path.relative_to(root_resolved)
    except Exception:
        return None
    if not path.is_file() or path.stat().st_size > 512 * 1024:
        return AgentSkillResource(name=name, path=rel_path, description=description, content="")
    try:
        content = path.read_text(encoding="utf-8", errors="replace")[:max_chars]
    except Exception:
        content = ""
    return AgentSkillResource(name=name, path=rel_path, description=description, content=content)


def _load_skill_json(path: Path, *, source: str, max_resource_chars: int) -> AgentSkill | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    root = path.parent
    instructions = str(data.get("instructions") or "").strip()
    if not instructions:
        for fallback_name in ["SKILL.md", "README.md"]:
            fallback_path = root / fallback_name
            if fallback_path.is_file():
                try:
                    _, body = _parse_frontmatter(fallback_path.read_text(encoding="utf-8", errors="replace"))
                    instructions = body.strip()
                    break
                except Exception:
                    pass
    resources = [
        resource
        for resource in (_resource_from_spec(root, item, max_chars=max_resource_chars) for item in data.get("resources") or [])
        if resource is not None
    ]
    name = _safe_name(data.get("name"), fallback=root.name)
    return AgentSkill(
        name=name,
        description=str(data.get("description") or "").strip()[:500],
        domain=_as_str_list(data.get("domain")),
        triggers=_as_str_list(data.get("triggers")),
        allowed_tools=_as_str_list(data.get("allowed_tools")),
        instructions=instructions[:12000],
        resources=resources,
        runnable=bool(data.get("runnable", True)),
        run_mode=str(data.get("run_mode") or "agent_guidance").strip()[:80] or "agent_guidance",
        source=source,
        path=str(root),
    )


def _load_skill_markdown(path: Path, *, source: str, max_resource_chars: int) -> AgentSkill | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    meta, body = _parse_frontmatter(raw)
    root = path.parent
    heading = _HEADING_RE.search(body)
    fallback_name = heading.group(1).strip() if heading else root.name
    resources = [
        resource
        for resource in (_resource_from_spec(root, item, max_chars=max_resource_chars) for item in meta.get("resources") or [])
        if resource is not None
    ]
    return AgentSkill(
        name=_safe_name(meta.get("name"), fallback=fallback_name),
        description=str(meta.get("description") or "").strip()[:500],
        domain=_as_str_list(meta.get("domain")),
        triggers=_as_str_list(meta.get("triggers")),
        allowed_tools=_as_str_list(meta.get("allowed_tools")),
        instructions=body.strip()[:12000],
        resources=resources,
        runnable=str(meta.get("runnable", "true")).strip().lower() not in {"0", "false", "no"},
        run_mode=str(meta.get("run_mode") or "agent_guidance").strip()[:80] or "agent_guidance",
        source=source,
        path=str(root),
    )


@dataclass(frozen=True)
class SkillRegistry:
    skills: tuple[AgentSkill, ...] = field(default_factory=tuple)

    @classmethod
    def load(
        cls,
        *,
        include_builtin: bool = True,
        include_user: bool = True,
        builtin_dir: Path | None = None,
        user_dir: Path | None = None,
        max_resource_chars: int = 2400,
    ) -> "SkillRegistry":
        items: list[AgentSkill] = []
        if include_builtin:
            items.extend(_load_skills_from_root(builtin_dir or default_builtin_skills_dir(), source="builtin", max_resource_chars=max_resource_chars))
        if include_user:
            items.extend(_load_skills_from_root(user_dir or default_user_skills_dir(), source="user", max_resource_chars=max_resource_chars))
        deduped: list[AgentSkill] = []
        seen: set[str] = set()
        for skill in items:
            key = skill.name.lower()
            if not key or key in seen:
                continue
            seen.add(key)
            deduped.append(skill)
        return cls(tuple(deduped))

    def list(self) -> list[AgentSkill]:
        return list(self.skills)

    def summaries(self) -> list[dict[str, Any]]:
        return [skill.summary() for skill in self.skills]

    def select(self, *, term: str, domain: str = "", context: str = "", limit: int = 4) -> list[AgentSkill]:
        term_l = term.lower()
        domain_l = domain.lower()
        context_l = context.lower()
        scored: list[tuple[int, AgentSkill]] = []
        for skill in self.skills:
            score = 0
            for value in skill.domain:
                value_l = value.lower()
                if value_l and (value_l in domain_l or value_l in context_l):
                    score += 4
            for value in skill.triggers:
                value_l = value.lower().strip()
                if _contains_trigger(term_l, value_l):
                    score += 5
                elif _contains_trigger(context_l, value_l):
                    score += 2
            if not skill.domain and not skill.triggers:
                score += 1
            if score > 0:
                scored.append((score, skill))
        scored.sort(key=lambda item: (-item[0], item[1].name.lower()))
        return [skill for _score, skill in scored[: max(0, limit)]]


def _load_skills_from_root(root: Path, *, source: str, max_resource_chars: int) -> list[AgentSkill]:
    if not root.exists() or not root.is_dir():
        return []
    out: list[AgentSkill] = []
    for child in sorted(root.iterdir(), key=lambda item: item.name.lower()):
        if not child.is_dir():
            continue
        json_path = child / "skill.json"
        md_path = child / "SKILL.md"
        skill: AgentSkill | None = None
        if json_path.is_file():
            skill = _load_skill_json(json_path, source=source, max_resource_chars=max_resource_chars)
        elif md_path.is_file():
            skill = _load_skill_markdown(md_path, source=source, max_resource_chars=max_resource_chars)
        if skill is not None:
            out.append(skill)
    return out
