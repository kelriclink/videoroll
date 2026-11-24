# Rust 原版独立服务启动模式

本批实现 Bilibili Publisher、Subtitle Service、YouTube Ingest 的独立 HTTP 入口。
运行模式分别为 `videoroll-v2 service bilibili-publisher`、`videoroll-v2 service subtitle-service`、`videoroll-v2 service youtube-ingest`。
每个进程必须设置独立的 `VIDEOROLL_V2_BIND`，默认仅允许 loopback；容器网络绑定需要显式启用 `VIDEOROLL_V2_SERVICE_ALLOW_CONTAINER_BIND=true`。
数据库、存储及 Rust 内部密钥沿用现有配置；`INTERNAL_API_SECRET` 用于派生 Python 原版 HMAC 内部服务令牌。

- GET /health 公开，返回原版 {"status":"ok"}。
- 其他请求必须通过原版内部服务 HMAC 鉴权，缺少令牌返回 401，错误令牌返回 403。
- 服务边界由原版路由清单与已登记的 Rust V1 适配器交集限定，不跨服务暴露任务管理、账户管理或其他业务接口。
- 复用现有原生处理器，不新增数据库表、不启动 worker、不转发 Python 请求。
- 健康检查仅表示 HTTP 服务存活，不代表外部平台、GPU 或全部业务行为通过验收。
- 修复运行时 /api/v2/parity 遗漏独立 Egress/SAU 接口计数的问题，与静态矩阵一致；接口级映射为 200/217，完整生产等价签收仍为 0。

验证：cargo check --locked --offline --all-targets 通过；cargo test --locked --offline --lib 173 项通过，包含真实 Router 的三服务健康检查、鉴权、跨服务阻断，以及审计一致性。未执行生产部署、真实投稿或生产数据库迁移。
