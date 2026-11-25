# Rust 隔离验收环境可复现性（2026-11-25）

基线 `32ec083`。此批仅修改测试基础设施和格式，不改变业务行为、数据库结构、API 或生产配置。

- 全量 smoke 中 57 处原 Python 差分解释器调用统一使用 `VIDEOROLL_V2_TEST_PYTHON`，未显式配置时兼容 link 现有 core venv，其他机器回退到 `python3`。无效解释器在创建容器前报错。
- 提前创建 `rust/target`，避免新工作树首次运行时 mock 日志和临时夹具目录不存在。
- GitHub Actions 隔离任务安装 Python 3.12 及原 Python 锁定依赖、Node 22 与 WebSocket 测试依赖、FFmpeg，并为 offline Cargo 子命令预取锁定依赖；保持所有原有检查，隔离任务时限延至 45 分钟。
- `rust/src/lib.rs` 仅调整 rustfmt 要求的模块声明顺序。

验证：`cargo fmt --check` 由失败变为通过；`bash -n`、契约矩阵检查（217/217 partial native）、`git diff --check`、Workflow YAML 解析、本机 Python 夹具依赖导入通过。无效解释器在数据库初始化前退出；使用显式 core venv 和独立端口 35469 的 `VIDEOROLL_V2_TEST_DB_CLEANUP_ONLY=true` 实测通过，测试容器/命名卷无残留。

限制：本轮没有重新运行全量业务 smoke、Clippy 或 GitHub 托管 Runner，不代表远端 CI 已通过；依赖安装、完整正向 API 及差分验证仍待远端与隔离环境确认。不操作生产服务、数据库、真实账号或外部平台。接口正向通过数不因测试基础设施变更而增加。
