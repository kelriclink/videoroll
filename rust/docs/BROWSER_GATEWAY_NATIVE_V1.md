# Orchestrator 原版浏览器兼容网关

将 Python Orchestrator 的三组通配路由（共 11 条声明）迁移至原生 Rust 进程内分发：

- Bilibili: GET / POST / PUT /bilibili/{service_path:path}
- Subtitle: GET / POST / PUT / DELETE /subtitle/{service_path:path}
- YouTube: GET / POST / PATCH / DELETE /youtube/{service_path:path}

实际 HTTP 入口为 /api/v1 下的相同路径。先经过已有管理员会话/内部令牌鉴权，再检查原 Python 各代理服务的白名单和动态参数规则，直接调用现有原生业务 Router。原方法、查询字符串、请求头及未读取的请求体不变；清除外层 Axum 通配路径参数，内层重新匹配具体业务路径。禁止任意 URL、跨服务路径、路径穿越、编码分隔符及无效编码，不调用 Python HTTP 服务。

原版白名单之外、此前已提供的显式 Rust 兼容 API（如独立投稿、知识导入、Embedding 模型管理及命名 B 站账号管理）仍按单独的精确规则保留；不是通用代理。原版允许而独立服务没有对应方法的请求返回 405，不伪造业务成功。独立服务 Router 继续保持原 HMAC 边界。

隔离测试覆盖原版白名单、恶意路径、请求体和查询参数、路径参数重新匹配、真实主 Router 的全部 11 条方法的未登录和非白名单拒绝，以及真实原生处理器的读取和参数验证。API 探测脚本区分独立服务健康检查，避免把主 API /health 重复统计；通配声明探测为拒绝路径，正向分发由专项测试覆盖。

接口矩阵 211/217 部分原生适配；仍缺 6 条独立 SAU 接口。真实生产行为等价签收仍为 0，未部署生产、未修改生产数据库。
