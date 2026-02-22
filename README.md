# videoroll

模块化视频处理流水线（合规版）：内容接入 → 字幕/翻译 →（可选）压制/封装 →（可选）投稿到哔哩哔哩。

详细规格：`docs/PROJECT_SPEC.md`

## Quickstart（后端骨架）

1) 准备环境变量：
- `cp .env.example .env`

2) 启动：
- `./scripts/dev_up.sh`（包含后端 + Web）

3) 核心服务端口：
- API（monolith）：`http://localhost:8000/docs`（`src/videoroll/apps/monolith/main.py`）
- Subtitle Service：`http://localhost:8000/subtitle-service/docs`
- YouTube Ingest：`http://localhost:8000/youtube-ingest/docs`
- Bilibili Publisher：`http://localhost:8000/bilibili-publisher/docs`
- Web UI：`http://localhost:3000`

## 最小流程（本地视频 → 字幕/硬字幕）

1) 创建任务（示例：本地自制/已授权内容）：
- `POST http://localhost:8000/tasks`

2) 上传视频：
- `POST http://localhost:8000/tasks/{task_id}/upload/video`（multipart file）

3) 触发字幕任务：
- `POST http://localhost:8000/tasks/{task_id}/actions/subtitle`

字幕任务产物会写入 MinIO（默认 bucket：`videoroll`），并在 `GET /tasks/{task_id}/assets` 可看到对应 `storage_key`。

## ASR 说明

- 默认启用 `SUBTITLE_ASR_ENGINE=faster-whisper`（默认模型：`SUBTITLE_WHISPER_MODEL=tiny`）。
- 模型管理：Web `Settings → ASR/Whisper`（`http://localhost:3000/settings/asr`），模型会落到本地目录 `data/models/whisper/`（容器内路径：`/models/whisper`）。
- 如需回退到示例字幕：将 `SUBTITLE_ASR_ENGINE=mock`。

## 翻译（可选：OpenAI）

- 任务详情页可开启翻译并选择 `translate_provider=openai`。
- 配置页：`http://localhost:3000/settings/translate`（可查看当前配置并测试）。
- OpenAI 配置在 Web 设置页保存（不再通过 `.env`）。密钥会加密存储在 DB 中，解密密钥保存在本地目录 `data/secrets/fernet.key`（容器内：`/secrets/fernet.key`）。

## 合规边界

本项目仅用于处理你拥有版权/已获授权/明确允许再分发的视频内容；不要用于批量搬运或抓取他人热门内容。
