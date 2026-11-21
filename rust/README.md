# VideoRoll 2.0 (Rust) — 独立重写分支

> **状态：可编译的基础架构，不是已经具备原版完整功能的生产替代品。**
>
> 此目录只在 `rewrite/rust-v2` 开发。不得直接替换 `/opt/videoroll/current`，不得让新旧 Worker 同时消费同一批可投稿任务。

## 整体迁移原则

- **原版 PostgreSQL 业务契约保持兼容**：`tasks`、`assets`、`subtitles`、`ingested_videos` 和 `app_settings` 作为过渡读取/登记表。新的工作流、任务、租约、事件、资产登记与垃圾回收只写入 `vr2_*` 表。
- **后端控制层 Rust**：Axum API、SQLx、Tokio Worker、持久任务、身份认证、文件流、投稿一致性保护。
- **耗时媒体执行用隔离 Worker**：FFmpeg、yt-dlp、aria2c、Whisper、VoxCPM2、Demucs 和后续模型服务不应运行于 API 的 Tokio 线程中。
- **Bilibili 等平台不可通过请求超时推断发布失败**：必须先核实外部稿件，恢复路径保留 `unknown` 状态。
- **不继承垃圾文件扫描即删除的管理方式**：所有 V2 资产必须登记、持引用/保留期限并在可删除时经过数据库 fence。v1 资源不可由 v2 GC 删除。

## 模块状态

| 范围 | 实际完成状态 |
|---|---|
| Rust 项目及工具链 | 完成，Cargo 构建、格式化与基础测试 |
| 任务数据库与提交 | 已实现独立 Schema、原子接入、来源去重、租约/幂等键、暂停/恢复/取消 |
| 任务恢复 | 已实现过期租约回收与投稿未知状态封锁；尚缺真实生产故障注入和 Hatchet 数据迁移 |
| API | 实现 v2 工作流与资产 API，以及 v1 管理员登录 Cookie 的兼容路径；原 v1 页面所需所有路由尚未覆盖 |
| YouTube 下载 | 已实现 yt-dlp 子进程和已有成品检查；未完成全部 v1 代理/aria2/进度细节 |
| 资源系统 | 已实现流式上传、单范围下载、SHA256、显式对象引用、fenced GC；尚需真实多 Worker 压力测试 |
| SRT、ASS | 解析与基础生成；缺原版可读性与 GPU 字幕叠加全部算法等价性 |
| ASR | 尚未迁移；计划使用独立 AI 推理 Worker |
| 翻译、RAG Agent、Embedding | 尚未迁移；原版上下文记忆、工具策略、知识库待做契约测试 |
| GPU 渲染 | 仅建立 Worker Gateway 与交付状态契约；Windows Rust Renderer / ffplayout 尚未与 v2 集成 |
| YouDub 配音、双 P | 尚未迁移；不能视为端到端可用 |
| Bilibili/其他平台投稿 | 状态模型与回执校验已实现，真实提交协议尚未迁移，禁止用旧生产账户试运行 |
| 前端 | 复用原版 React 代码，尚未完成 v2 API 全面适配和 UI 验收 |

## 模块目录

- `src/domain.rs`：阶段、工作流、资产与状态类型
- `src/database.rs`：原版业务表兼容与 SQLx
- `src/scheduler.rs`：持久工作流、CAS/租约、防重、对账、恢复
- `src/api.rs` / `src/auth.rs`：Axum API 与管理员认证
- `src/ingest.rs`：YouTube 单视频 URL 与 yt-dlp
- `src/executor.rs`：阶段执行器和 Gateway 调度
- `src/assets.rs` / `src/storage.rs`：视频流、对象引用、GC
- `src/subtitles.rs`：字幕格式
- `src/publish.rs`：投稿回执与批次状态规则

## 开发/验收

使用隔离 Postgres 数据库；示例命令仅在**显式配置的开发数据库**下执行：

```bash
cd rust
cargo fmt --check
cargo check --all-targets
cargo test --all
```

在 link 上可运行 `bash rust/scripts/smoke-isolated.sh`，脚本使用一次性的本地 Docker PostgreSQL 容器和回环地址，测试结束自动回收，绝不使用 v1 正式数据库。

启动命令支持 `migrate` / `api` / `worker` / `gc`，不会在 API 启动时自动执行迁移，也不会默认启动 Worker。

关键环境变量：

| 环境变量 | 用途 |
|---|---|
| `DATABASE_URL` | V2 专用测试数据库；禁止直接接入正式实例 |
| `VIDEOROLL_V2_BIND` | 回环监听地址；默认 `127.0.0.1:18080` |
| `VIDEOROLL_V2_STORAGE_ROOT` | V2 隔离文件根目录 |
| `VIDEOROLL_V2_INTERNAL_TOKEN` | 长度至少 32 的 v2 内部 API Token |
| `INTERNAL_API_SECRET` | 可选，兼容原版受信任管理员 Cookie |
| `ADMIN_BOOTSTRAP_SECRET` | 可选，首次创建管理员密码时使用 |
| `VIDEOROLL_V2_WORKER_GATEWAY` | 独立 Worker Gateway 地址，未设置时字幕、配音、投稿阶段明确失败 |
| `VIDEOROLL_V2_YT_DLP` | yt-dlp 可执行文件路径 |
| `VIDEOROLL_V2_WORKERS` | 后台 Worker 数量 |

## 外部 Worker 最小协议

V2 对非下载阶段向 `POST {GATEWAY}/v2/stages/{stage}` 发送：

```json
{
  "job_id": "UUID",
  "workflow_id": "UUID",
  "task_id": "UUID",
  "operation_key": "vr2:...",
  "payload": {}
}
```

Gateway 必须鉴权 `x-videoroll-v2-internal-token`，并返回其自己的持久化任务凭据。同步完成可返回 `{"status":"completed","result":{...}}`；异步接受返回 HTTP 202 和 `{"status":"accepted","ticket":"..."}`。异步处理完成后带相同 Ticket 回调 `/api/v2/jobs/{id}/complete`。**收到 202 只代表接受请求，不能当成处理完成**。投稿结果还必须有明确回执 `{"confirmed":true,"external_id":"..."}`。

当前尚不存在上线可用的 Gateway 实现，也没有真实投稿协议，因此不得启动生产 Worker。

## 待完成的生产门槛

1. 完成全部 217 个原 API 路由语义对比与前端兼容测试。
2. 完成 YouTube 代理与 aria2 的真实下载恢复测试，时间戳、日志与进度保持一致。
3. 将原版 Python ASR/LLM/YouDub 等实现封装成稳定的媒体 Worker 接口，再逐步替换 Rust 可实现部分。
4. 衔接原版 Windows Rust Render Worker、ffplayout 与新资产协议。
5. 完成 Bilibili 原生双 P、字幕上传、分区、话题活动验证、未知投稿人工核实；完成社交平台 SAU。
6. 使用真实但隔离的媒体测试资源做完整下载→字幕→翻译→渲染→配音→投稿→清理，检查重新启动/中断/重试/资源残留。
7. 核实长期任务租约、资源竞争和多进程部署负载，再经过只读影子运行后才考虑切换。

**本次没有进行生产迁移或部署，也没有声称完成原版全部功能。**
