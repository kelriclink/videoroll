# VideoRoll 文档索引

本目录以 **2026-09-20 当前代码实现**为基线。文档描述“系统现在如何工作”，不再把早期设计建议与已实现能力混在一起。

## 建议阅读顺序

1. [../README.md](../README.md) — 项目概览与快速开始
2. [PROJECT_SPEC.md](PROJECT_SPEC.md) — 当前产品能力和领域模型
3. [ARCHITECTURE.md](ARCHITECTURE.md) — 服务、网络和可靠性架构
4. [DEPLOYMENT.md](DEPLOYMENT.md) — 部署与运维
5. [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) — 开发、测试和扩展
6. 专项文档：
   - [REMOTE_API.md](REMOTE_API.md)
   - [realtime-events.md](realtime-events.md)
   - [AGENT_SKILLS.md](AGENT_SKILLS.md)
   - [social-publisher.md](social-publisher.md)
   - [SECURITY_AUDIT.md](SECURITY_AUDIT.md)
   - [REFERENCES.md](REFERENCES.md)

## 文档职责

| 文档 | 主要职责 |
|---|---|
| 根目录 README | 项目定位、能力、快速开始 |
| PROJECT_SPEC | 当前业务能力、领域实体、状态和产物契约 |
| ARCHITECTURE | 服务边界、网络、内部认证、可靠性与数据流 |
| DEPLOYMENT | 环境、目录、迁移、发布、GPU、验证和回滚 |
| DEVELOPER_GUIDE | 代码组织、开发规则、测试与 CI |
| REMOTE_API | 外部自动接入 API |
| realtime-events | WebSocket topics、事件和恢复语义 |
| AGENT_SKILLS | Skill 格式、选择与工具边界 |
| social-publisher | 社交平台账号、浏览器登录和投稿 |
| SECURITY_AUDIT | 当前安全控制与剩余风险 |
| REFERENCES | 上游项目和运行/参考依赖 |

## 事实来源优先级

文档与代码冲突时，以以下顺序为准：

1. 数据库 migration 与 SQLAlchemy model；
2. FastAPI route、service 和 worker 实现；
3. `docker-compose.yml` / `docker-compose.intel.yml`；
4. 前端 API 调用与页面行为；
5. `.env.example`；
6. 本目录文档。

修改以下内容时，应在同一提交更新相应文档：

- 服务或端口；
- API route；
- 数据库 migration；
- 持久化目录；
- 安全边界；
- Agent/翻译数据流；
- 生产部署和回滚步骤。
