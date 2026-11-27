# 第二十二批：全仓 Python 复查与登录安全

基线 924235e。重新执行 `audit-python-behavior.py --all-python`，读取全部 1207 个 Git 跟踪 Python 文件（238733 行、10593 个定义、零 AST 解析错误）。第一方业务 201 文件/65413 行/2710 定义，第一方部署/桥接 15、历史迁移 14、测试与审计 239、第三方快照 738。按目录统计并复核第十一至二十一批的差异记录。临时清单 /tmp/vr-b22-python-inventory.json 和 .csv；可由审计脚本重新生成，不重复提交整份大型清单。**全仓静态索引不是每个函数的逐行语义签收**。

## 实际对照
- 原 `apps/security/trusted_proxies.py`：显式 TRUSTED_PROXY_CIDRS/HOSTS、60 秒 DNS 窗口、从右向左移除可信代理。旧 Rust 仅无条件信任 loopback 的 X-Real-IP，非 loopback 代理不能识别真实来源。
- Rust 只信任明确配置的网段/主机，默认空集合；不信任 X-Real-IP。IPv4/IPv6、非规范 CIDR 网络地址、异常转发链回退；多行 XFF、超过 4096 字节或 64 跳拒绝解析。DNS 只解析配置主机，每次最多 32 个，四个并发、总预算 2 秒；共享刷新窗口，不信任失败的旧缓存。新部署示例显式配置本地 Nginx。配置更改需要重启 API。若 API 可被其他本机进程直接访问，允许回环代理等同信任这些进程；网络隔离仍必要。
- 原 `apps/security/rate_limits.py`：连续 5 次失败、滚动 60 秒、初始 120 秒锁定、后续请求指数增长至 3600 秒、429 Retry-After。旧 Rust 为 8 次、15 分钟固定窗口、10 分钟封禁，无 Retry-After，检查与失败计数不原子。
- 新 Rust 复用 auth_login_limits，不新增表、不引入 Redis。验证前原子预占尝试次数，按源地址散列、在短事务内行锁计数，第五次正确密码仍允许并清空计数，第五次失败和此后锁定请求均返回 429。崩溃或取消验证消耗预占次数，避免并发绕过；这是有意更保守的差异。最多八个进程内密码验证槽，PBKDF2 在阻塞线程执行，槽满返回 Retry-After: 1。共享限流在 PostgreSQL 故障时拒绝验证，503。登录成功保留原 cookie 契约；两个版本入口共用同一桶。
- 本批只修改 admin 登录入口；setup 独立限流、原 security_audit_events 写入、过期 PostgreSQL 限流桶定期回收仍需补齐。不同进程分别限制 PBKDF2 并发；没有生产高并发性能测量，未验证真实 Docker DNS 换址。

## 继续深挖的范围
P0：publish_lifecycle、social_publisher、publish_service 的历史批次与目标级 unknown 回执，避免状态覆写和重复投稿；P1：auth_service 的 setup/安全审计/过期桶、filesystem 的 sibling .partial 文件、SAU 账户级并发与凭据更新、工作流与历史渲染失败/取消的安全回收。第三方快照作为依赖边界处理，不重写其模型及浏览器运行时。

## 第二十三批：初始化、安全审计与限流回收

基线 df70255。完整对照 Python auth_service 的 setup/login 处理、security/audit 的字段白名单、rate_limits 的 TTL 与 service_auth 的一次性初始化授权。保留 Rust 既有初始化 HTTP 契约（成功 201/trusted=false、重复 409、错误授权 401；与 Python 的 200/trusted=true、400、403 仍不同），不在本批静默改变调用者行为。

- 登录保留已有 SHA256(IP) 桶键；初始化使用 SHA256("setup:"+IP)，两个版本入口共享对应动作桶。共享 PBKDF2 并发上限，初始化哈希同样移到阻塞线程。第五次正确请求可成功，失败进入 429。
- 原 security_audit_events 表写入固定事件、规范 IP、结果、固定错误码及 attempts/retry_after 数值。无原始请求头、密码、Cookie、bootstrap token、request_id 或异常正文。登录成功的限流清除与审计同事务；初始化成功还与密码及 bootstrap_consumed 更新同事务。审计写入失败阻止成功响应。限流存储故障尽力保留审计，数据库完全不可用时无法持久化；CPU 槽位饱和和请求解析失败不保证有审计记录。
- 每 300 秒最多回收 1000 个已超过实际过期时间一小时的桶；行锁 SKIP LOCKED，删除前再检查过期。已有 gc CLI 也执行有界回收，不删除审计历史。未增加生产业务表或迁移；隔离 schema 补上原版 security_audit_events。
- 专项隔离测试扩展：登录/初始化独立桶、错误授权、未设置密码、无效密码、审计故障事务回滚、竞争初始化仅一次成功、敏感字段不入库、过期桶回收与锁保护。未使用生产库或真实账号。
