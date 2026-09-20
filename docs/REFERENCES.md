# VideoRoll 上游与参考项目

VideoRoll 不在主仓库复制无关上游项目的完整快照。运行依赖使用 package pin、Git submodule 或独立集成目录。

| Project | Upstream / location | VideoRoll 用途 |
|---|---|---|
| social-auto-upload | `.gitmodules` → `git@github.com:kelriclink/social-auto-upload.git` | 抖音/小红书/快手浏览器投稿运行依赖 |
| ffplayout | `services/ffplayout/` | 播控引擎与 VideoRoll 集成 |
| biliup | https://github.com/biliup/biliup | Bilibili 行为/实现参考，不是运行 import |
| bilibili-API-collect | https://github.com/SocialSisterYi/bilibili-API-collect | Bilibili API 文档参考 |
| yt-dlp | Python dependency | YouTube metadata/download |
| pgvector | PostgreSQL extension | 向量存储与检索 |
| OpenVINO / OpenVINO GenAI | Python/runtime dependency | Intel ASR / Embedding |
| faster-whisper | optional Python dependency | 本地 ASR |
| sentence-transformers | subtitle image dependency | 本地 Embedding |

## 使用原则

引用上游实现时优先：

1. 链接上游文件、文档或 issue；
2. 本地只保存必要适配；
3. 固定运行版本或 submodule commit；
4. 升级后运行 CI、smoke 和生产前验证；
5. 不因为“参考过某项目”就把整个仓库复制进 VideoRoll。

## social-auto-upload

这是当前明确的 Git submodule 运行依赖。

初始化：

```bash
git submodule update --init --recursive
```

生产镜像构建需要它存在。

## ffplayout

ffplayout 在：

```text
services/ffplayout/
```

中以 VideoRoll 集成形式构建。

部署和浏览器访问都通过 VideoRoll 自己的 Compose/Nginx 配置，不建议额外维护第二套独立 ffplayout 容器定义。

## 版本升级

第三方版本升级至少关注：

- API contract；
- DB/schema；
- browser selector；
- model/runtime compatibility；
- image size；
- network/permission requirements；
- license。

依赖版本事实以 `pyproject.toml`、lock file、Dockerfile 和 submodule commit 为准。
