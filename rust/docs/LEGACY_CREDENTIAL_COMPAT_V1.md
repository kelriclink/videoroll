# 原生产历史凭据兼容（2026-11-25）

Rust 保留原 PostgreSQL 凭据格式，支持 dbplain:v1、原有未加前缀值及历史 Fernet。历史密文先验证 HMAC，再用 OpenSSL EVP AES-128-CBC 解密及验证 PKCS#7 padding；不将密钥传给子进程，不自动改写原密文。读取原 VIDEOROLL_FERNET_KEY_PATH 或 VIDEOROLL_DATA_ROOT/secrets/fernet.key，绝对路径、服务用户所有、不可被组/其他用户读取、拒绝符号链接和非普通文件、文件大小有界。密钥错误、密文损坏、文件不安全时失败关闭。限制明文 1 MiB，普通 HTTP 密钥仍受原长度和单行校验。构建依赖 libssl-dev，运行依赖 libcrypto.so.3。

接入原 ASR、翻译、Embedding、远程模型目录、B 站凭据和 SAU 账号状态。YouTube 原数据库 Cookie 解密后通过原格式校验，仅供内存/每次下载的私有临时 Cookie 文件使用；不自动创建持久明文文件。显式替换或清空时移除旧字段，避免旧凭据复活，保留 Rust 原本的私有文件写入策略。SAU 成功检查后沿用已有刷新行为，不声称新加密存储。dbplain:v1 不是加密。

测试均使用合成密钥及 mock 平台，未读取真实密钥内容、未实际解密生产密文或调用真实平台。
- cargo check --all-targets 通过，库测试 182/182；Python cryptography 生成的已知测试密文、错误密钥、篡改、非法密钥权限与符号链接。
- credential-compat-bili.log：原加密 B 站 Cookie 进入真实回环 HTTP，原密文不变；原加密翻译 API key 进入模拟 OpenAI Bearer 请求，原 Index/分区推荐等差分通过。专项测试修正缺失 mock 端口环境及大小写敏感断言。
- credential-compat-home.log：原加密多行 YouTube Cookie 进入模拟 Innertube，完整 Home 手动/定时、锁、去重、异常恢复、显式替换/清空通过；恢复后续测试夹具，未遗留测试 Cookie。
- credential-compat-sau.log：历史加密多行账号 JSON 进入模拟 SAU CLI，检查及刷新成功，临时状态文件回收；并发、失败、超时、崩溃恢复回归通过。
- 生产密钥文件仅核对路径和文件权限；原密文实际可否由该密钥解开、生产 API 凭据有效性、真实平台业务未验收。部署时须显式配置原密钥路径或原数据根，并核实服务用户权限。
