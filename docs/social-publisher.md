# 社交平台投稿

VideoRoll 使用独立的 `social-publisher-api` 和 `social-publisher-worker` 容器调用
`social-auto-upload`（SAU）CLI。Chromium、Patchright、账号文件和平台页面选择器不会进入主应用进程。

首版支持抖音、小红书和快手的视频投稿，并提供服务器浏览器登录窗口。登录窗口通过
Xvfb、x11vnc 和 noVNC 在网页中打开，可处理扫码、短信验证和平台安全确认。

## 准备代码

SAU 以固定 Git submodule 提交随项目构建：

```bash
git submodule update --init --recursive
```

## Docker 配置

配置浏览器发布运行参数：

```dotenv
SOCIAL_PUBLISHER_URL=http://social-publisher-api:8010
SOCIAL_PUBLISH_CONCURRENCY=1
SAU_HEADLESS=true
DOUYIN_COOKIE_AUTH_HEADLESS=true
SAU_ACCOUNT_CHECK_TIMEOUT_SECONDS=120
SAU_UPLOAD_TIMEOUT_SECONDS=3600
SOCIAL_LOGIN_TIMEOUT_SECONDS=900
```

构建并启动：

```bash
docker compose build social-publisher-api social-publisher-worker
docker compose up -d social-publisher-api social-publisher-worker app web
```

两个 social-publisher 服务不映射宿主机端口，只通过 Compose 内网与 Orchestrator 通信。
Web 容器把 `/social-login/` 反向代理到 API 容器的 noVNC 服务。
任务详情页的“打开自动化窗口”按钮把 `/social-publish/` 反向代理到 worker 容器的 noVNC 服务；
抖音投稿使用有头 Chromium，因此可以实时观察上传和发布过程。

## 网页浏览器登录

打开“投稿设置”，填写账号名后点击“网页登录”。VideoRoll 会：

1. 在 API 容器的临时 Xvfb 桌面启动 SAU 有头 Chromium。
2. 在新窗口打开 noVNC 页面，显示真实平台登录页面。
3. 允许用户扫码，并在出现短信或安全校验时直接输入和点击。
4. 登录成功后读取完整 storage_state，使用 Fernet 加密保存到数据库。
5. 删除容器中的临时明文账号文件。

同一时间只允许一个浏览器登录会话，默认 15 分钟超时。登录窗口仅用于账号登录和验证；
正常检查与投稿仍由独立 worker 使用无头浏览器执行。

## 在本地生成账号文件

在本地 `social-auto-upload` 目录安装并登录：

```bash
uv pip install -e .
patchright install chromium
sau douyin login --account creator
sau xiaohongshu login --account creator
sau kuaishou login --account creator
```

登录成功后得到：

```text
cookies/douyin_creator.json
cookies/xiaohongshu_creator.json
cookies/kuaishou_creator.json
```

这些文件是 Playwright/Patchright `storage_state` JSON，可能同时包含 cookies 与 origins/localStorage。
浏览器复制的普通 Cookie 字符串不能可靠替代它们。

## 手动文件导入

打开“投稿设置”，在对应平台卡片中填写相同账号名并上传 JSON。系统会：

1. 检查 JSON 格式和 1 MiB 大小限制。
2. 使用 `data/secrets/fernet.key` 加密保存到数据库。
3. 将账号校验任务发送给独立 worker。
4. 在网页显示 `queued`、`checking`、`valid`、`invalid` 或 `error`。

账号 JSON 不进入 S3，API 不会回显内容。worker 只在 tmpfs 中临时解密，并在命令结束后删除明文文件。

不要删除或替换 `data/secrets/fernet.key`；否则已加密的账号和其他加密设置将无法读取。

## 开启真实投稿

社交平台不再使用环境变量切换模拟/真实模式。账号校验成功后，在网页“投稿设置”中勾选对应平台，
该平台才允许进入投稿流程；取消勾选后，Orchestrator 会拒绝新的投稿请求。

建议每个平台先使用非生产账号进行一次人工监督投稿。

## 投稿状态

- `submitting`：排队、准备素材或浏览器正在执行。
- `submitted`：SAU 正常完成提交动作，但尚未取得平台作品 ID。
- `unknown`：浏览器启动后超时或异常，结果无法确认。
- `failed`：浏览器启动前的确定性错误。
- `published`：未来获得平台作品 ID 或完成独立确认后使用。

`submitted` 和 `unknown` 都不会自动重试。再次投稿前必须先到平台创作者后台确认，避免重复发布。
