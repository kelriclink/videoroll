# Rust 原生 Egress Gateway（独立服务）

取代 Python videoroll.apps.egress_gateway.main 的 GET /health、POST /fetch，
保留原内部 HMAC 服务 Token 协议和 Python FetchRequest/FetchResponse 主要字段。

- 独立二进制：videoroll-egress；不需要主 VideoRoll 数据库和管理 Cookie，
  也不开放到 /api/v1 主 HTTP Router。默认监听 127.0.0.1:8020，
  容器中如确需 0.0.0.0 监听，必须显式启用
  VIDEOROLL_V2_EGRESS_ALLOW_CONTAINER_BIND=true，并由内部 Docker 网络和
  防火墙限制访问，绝不能把本服务直接映射到公网。
- 必须提供非默认 INTERNAL_API_SECRET，令牌使用原
  HMAC-SHA256("videoroll-internal-service:v1") + v1. 前缀。
  /health 公开，/fetch 必须以 X-Videoroll-Internal-Token 提供该令牌，
  且先于 JSON 解析验证；普通管理员令牌、Cookie 不可替代。
- /fetch 仅 GET 指定 HTTP(S) 网页，端口 HTTP 80 / HTTPS 443，
  禁用 HTTP 代理与自动重定向；DNS 查询全部结果必须为公网 IP。
  使用 reqwest Client.resolve 将 TCP 连接绑定到 DNS 验证过的 IP，
  并校验可获得的实际远程连接地址，保留原始 HTTPS SNI 与证书验证。
- 每次收到 301/302/303/307/308 时，目标 URL 都重新检查协议、
  端口、DNS/IP、请求限制；最大 5 次重定向。
- 拒绝回环、RFC1918、链路本地、CGNAT、IMDS 169.254.169.254、
  IPv4 文档地址、IPv6 ULA/链路本地/过渡与保留地址。
  地址分类比 Python is_global 更保守，可能拒绝某些特殊的全球路由地址。
- 过滤 Cookie、Authorization、Host、代理和跳转等敏感请求头；
  拒绝 CRLF；限制至多 16 个自定义头，单值最大 1024 字符。
- 原限额：0.1–60 秒、1–2,000,000 字节、0–5 跳，
  响应保留状态码、响应头、最终 URL、Base64 body、truncated。
  只允许 text/*、application/json、+json、XML、+xml，
  拒绝下载二进制媒体与 HTML 外的不受支持内容。

测试：
- rust/src 单元测试：私网、保留地址、URL、非法头、HMAC。
- VIDEOROLL_V2_TEST_EGRESS_ONLY=true bash rust/scripts/smoke-isolated.sh
  测试独立 Rust 进程、动态 Python HTTP 本机模拟站点、HMAC 与权限、
  无数据库启动、请求头过滤、大小截断、Redirect SSRF、Content-Type、
  错误参数和 URL。这个构建使用**非默认的 Cargo
  egress-test-loopback 特性**并且必须同时设置两个测试守卫，
  仅允许 127.0.0.1:18098 作为测试源。
- 正式构建不启用该特性，本机/内网端口不会开放。正式行为需要额外
  验证生产 DNS、双栈、多地址重绑定、TLS 以及网络拓扑。

这是接口**部分迁移**，未做真实 Internet 或生产全链路等价签收。
Route Matrix 独立登记 2 条 Gateway 接口，主 API 的隔离探测只覆盖
原路由 189 条；Gateway 2 条由独立专项测试覆盖。
