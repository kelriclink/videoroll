# B 站账号及只读投稿契约：原表原位替换

2026-11-23：新增原生 V1 适配 5 条接口：

- `GET/PUT /bilibili/auth/settings`：唯一来源 `app_settings['bilibili.auth']`。使用 Python 相同的 `cookie_enc`、`sessdata_enc`、`bili_jct_enc` 字段及 `dbplain:v1:` 前缀；保留未知 JSON 字段，GET 只返回布尔标志，不暴露凭据。空 Cookie 清理三种凭据，单个字段空字符串仅清理该字段。
- `GET /bilibili/auth/me`：验证 Creator Cookie 后从 B 站账户 API 获取公开身份，原 DTO `mid/uname/userid/sign/rank`。
- `GET /bilibili/archive/types`：从 B 站投稿预检 API 获取多级分区，保留节点扩展字段，补足 `children` 缺省值。
- `GET /bilibili/publish/jobs/{job_id}`：原 `publish_jobs` 行只读，不创建 V2 影子作业，也不隐藏 Python 历史记录。

原生 B 站投稿、P2 追加和投稿后原生字幕同步统一使用上述数据库配置作为凭据来源；当旧部署未设置原数据库凭据时，才回退到 `VIDEOROLL_V2_BILIBILI_COOKIE`/`VIDEOROLL_V2_BILIBILI_CSRF`。不可恢复的旧 Fernet 凭据不能被环境变量静默掩盖，须在受控数据库中完成安全恢复或重新设置。

安全边界：主程序显式开启原生投稿前不会实际投递；B 站 API 请求不跟随 302 跳转；测试替换目标只能是显式允许的 loopback HTTP 端点。全部测试使用临时 PostgreSQL、模拟 B 站站点，**没有真实账号、真实投稿或生产 DB DDL**。

验收证据：`rust/tests/bilibili_account_v1_isolated.py` 和 `rust/tests/isolated-bili-account.sh` 使用原版 Python `get_bilibili_auth_settings` 及 `PublishJobRead` 做同表差分；有本地 Mock 的 Cookie、GET 账户、GET 分区、缺失 Cookie 和失效投稿作业边界。完整 `smoke-isolated.sh` 可通过 `VIDEOROLL_V2_TEST_BILIBILI_ACCOUNT_ONLY=true` 复用此测试。

未完成：`POST /bilibili/archive/type/recommend` AI 分区推荐、`POST /bilibili/publish` 完整旧业务契约、真实 B 站账号/代理/网络异常、平台回执与重启故障注入验收。五条路由只是部分原生适配，不是生产级全签收。
