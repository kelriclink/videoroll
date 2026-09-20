# VideoRoll Agent Skills

Agent Skill 是 RAG 子 Agent 的**指导与工具约束包**。

它不是 Python plugin，也不能直接执行 shell/script。

## 1. 目录

Built-in：

```text
src/videoroll/apps/subtitle_service/skills/
```

User：

```text
data/agent_skills/
```

可通过：

```dotenv
VIDEOROLL_AGENT_SKILLS_DIR=/custom/path
```

覆盖用户 Skill 根目录。

每个 Skill 使用独立目录。

## 2. 支持格式

每个目录可以包含：

```text
skill.json
```

或：

```text
SKILL.md
```

如果 JSON 没有 `instructions`，loader 会尝试读取同目录 `SKILL.md` 或 `README.md` 作为说明正文。

## 3. skill.json

```json
{
  "name": "hardware-terms",
  "description": "Research hardware terms with official sources first.",
  "domain": ["hardware", "electronics"],
  "triggers": ["HDMI", "VGA", "pinout"],
  "allowed_tools": ["search_web", "fetch_url"],
  "runnable": true,
  "run_mode": "agent_guidance",
  "instructions": "Prefer vendor manuals and standards.",
  "resources": [
    {
      "name": "source-policy",
      "path": "source-policy.md",
      "description": "Preferred source order"
    }
  ]
}
```

## 4. SKILL.md

```markdown
---
name: chemistry-terms
description: Research chemistry subtitle terminology.
domain:
  - chemistry
triggers:
  - reflux
  - condenser
allowed_tools:
  - search_web
  - fetch_url
runnable: true
run_mode: agent_guidance
---
# Chemistry Terms

Prefer IUPAC, SDS, textbooks and university sources.
```

Frontmatter parser 是轻量实现，不是完整 YAML engine；metadata 应保持简单。

## 5. 字段

| 字段 | 说明 |
|---|---|
| `name` | Skill 名称，同名会去重 |
| `description` | UI/选择说明 |
| `domain` | 匹配领域 |
| `triggers` | term/context 触发词 |
| `allowed_tools` | 可暴露给子 Agent 的工具限制 |
| `instructions` | prompt guidance |
| `resources` | 同目录附加文本资源 |
| `runnable` | 是否允许选择 |
| `run_mode` | 当前运行模式应为 `agent_guidance` |

## 6. Resource 安全边界

Resource path 必须解析在 Skill 自己目录内。

例如：

```text
../../secret
```

不会被当作合法资源读取。

当前还有：

- 单 resource 大文件限制；
- prompt resource 长度截断；
- instruction 长度截断；
- 最多只把有限数量 resource 注入模型。

Skill 目录路径属于运行 metadata，不会作为模型上下文直接泄漏。

## 7. Trust

Builtin Skill 在 prompt 中标记：

```text
trusted_builtin
```

User Skill 标记：

```text
untrusted_user_guidance
```

这意味着用户 Skill 可以提供领域提示，但不能覆盖系统工具策略、预算、安全 guardrail 或 egress policy。

## 8. Tool 约束

`allowed_tools` 为空时，不额外缩小当前 runtime 已允许的工具集合。

设置后，子 Agent 只能看到匹配的受控工具集合以及运行时必须的基础工具。

Skill 不能：

- 创建未注册工具；
- 打开系统禁用的工具；
- 绕过 `AgentRuntime`；
- 获得 shell/code execution。

## 9. Selection

`SkillRegistry.select()` 按以下信号计分：

- domain 与当前 domain/context 匹配；
- trigger 命中 term；
- trigger 命中 context；
- 无 domain/trigger 的通用 Skill 低权重匹配。

当前只选择少量高分 Skill，避免 prompt 无界膨胀。

只有：

```text
runnable = true
run_mode = agent_guidance
```

的 Skill 会进入选择。

## 10. 常见 Agent 工具

具体注册工具受运行配置影响，常见包括：

- `rag_lookup`；
- `wiki_search`；
- `search_web`；
- `fetch_url`；
- `finish`。

每次工具执行仍经过：

- Pydantic schema validation；
- Agent Runtime budget；
- timeout；
- cancellation；
- provider/egress rate limit；
- input/output guardrail。

## 11. 编写建议

Skill 应描述：

- 术语判断规则；
- 首选权威来源；
- 领域歧义；
- 输出格式与验证要求。

不要写入：

- API Key；
- Cookie；
- private secret URL；
- 要求绕过 egress/工具策略的指令；
- “必须执行某 shell command”之类伪插件行为。

## 12. 调试

API：

```text
GET /subtitle/agent/skills
```

会返回加载后的 Skill summary。

Agent trace 中可观察 Skill activation 与后续 tool call。
