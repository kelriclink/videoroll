# VideoRoll

模块化视频处理流水线：YouTube 接入 → 语音识别/翻译 → 压制封装 → B 站投稿。

核心特性是 **RAG Agent 驱动的翻译知识库**：翻译每个字幕前，LLM 自动判断哪些术语需要外部查证，检索可信来源后注入翻译上下文。解决动漫、游戏实况、技术讲解等视频中“模型缺上下文”的问题。

---

## 功能概览

- **RAG Agent 翻译** — 主 Agent 术语 gate → 子 Agent 并发研究（Wikipedia / 网络搜索 / 网页抓取）→ verifier 校验 → 写入 pgvector 知识库 → 精简术语卡片注入翻译 prompt
- **多格式 ASR** — faster-whisper（CPU/OpenVINO）、OpenVINO GPU 加速，可在 Web UI 切换
- **字幕翻译 + 压制** — 硬字幕 burn-in 或软字幕 soft-sub，多语言支持
- **YouTube 自动流水线** — 下载 → 字幕 → 翻译 → 压制 → 可选投稿 B 站，一键完成
- **Bilibili 投稿** — Web UI 配置 Cookie、投稿模板，支持分区/标签/封面
- **Web 管理面板** — 任务管理、Dashboard 资源监控、Agent 运行树、RAG 知识库管理、全局设置
- **词典导入** — 支持 CSV、TSV、TMX、TBX、JSONL、CC-CEDICT、ECDICT 等格式，导入术语到 pgvector 知识库

## 架构

```
┌──────────────────────────────────────────────────────────┐
│  web (React + nginx)            PostgreSQL 16+ (外部)    │
│  └─ /api → app (FastAPI monolith)                        │
│      ├─ orchestrator    (任务 / 资产 / 认证 / 设置)      │
│      ├─ subtitle-service (ASR / 翻译 / RAG / 压制)      │
│      ├─ youtube-ingest   (频道扫描 / 下载)               │
│      └─ bilibili-publisher (投稿 / 上传)                 │
│      + 2 Celery workers (subtitle / publish)             │
│  Redis (broker)          MinIO (对象存储)                │
└──────────────────────────────────────────────────────────┘
```

## 环境要求

- Docker Engine + Docker Compose Plugin
- PostgreSQL 16+（需启用 `pgvector` 扩展）
- 可访问 YouTube 的网络环境

可选：

- Intel iGPU（用于 OpenVINO ASR / 硬件转码加速）

## 快速开始

### 1. 克隆仓库

```bash
git clone git@github.com:kelriclink/videoroll.git
cd videoroll
```

### 2. 准备 PostgreSQL

```sql
CREATE DATABASE videoroll;
CREATE EXTENSION IF NOT EXISTS vector;
```

### 3. 配置环境变量

```bash
cp deploy_compose/.env.example deploy_compose/.env
# 编辑 deploy_compose/.env
# 至少修改 DATABASE_URL 指向你的 PostgreSQL
```

关键配置：

| 变量 | 说明 |
|---|---|
| `DATABASE_URL` | 数据库连接，Docker 环境用 `host.docker.internal` |
| `SUBTITLE_ASR_ENGINE` | ASR 引擎：`faster-whisper`（默认）、`openvino`、`mock` |
| `SUBTITLE_WHISPER_MODEL` | Whisper 模型：`tiny`、`base`、`small`、`medium`、`large` |

完整配置见 `deploy_compose/.env.example`。

### 4. Docker 部署

```bash
# 本地开发
./scripts/dev_up.sh

# 构建生产镜像
bash scripts/build_export_prod.sh
# 输出: videoroll-prod-bundle-<timestamp>.tar

# 在目标机器导入镜像
docker load -i videoroll-prod-bundle-*.tar

# 使用 compose 启动
docker compose -f deploy_compose/docker-compose.yml up -d
```

### 5. 初始化

首次访问 Web UI 会要求设置管理员密码。然后在 `Settings` 中配置：

- **Translate** — LLM API key、翻译模型、embedding、RAG Agent 参数
- **YouTube** — 可选代理、cookies
- **Bilibili** — Cookie、投稿模板
- **Auto** — 自动模式默认参数（一键完成全流程）

## 文档索引

| 文档 | 说明 |
|---|---|
| [项目规格](docs/PROJECT_SPEC.md) | 详细能力说明、模块拆分、RAG 翻译流程 |
| [安全审计](docs/SECURITY_AUDIT.md) | 安全审计报告及改进建议 |
| [Agent Skills](docs/AGENT_SKILLS.md) | Agent 能力包格式与自定义指南 |
| [开发者指南](docs/DEVELOPER_GUIDE.md) | 本地开发、调试、代码组织 |
| [远程 API](docs/REMOTE_API.md) | 远程自动提交 API 说明 |

## 合规声明

本项目仅用于处理你拥有版权、已获授权、或明确允许再分发的视频内容。不要将其用于批量搬运、绕过平台限制、或处理无授权内容。
