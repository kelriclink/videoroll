# 本地模型常驻与并发保护

基线 91824c3。Rust 仍掌握任务、配置、超时、存储和数据库；Python 仅作为第三方模型运行适配器。没有新增业务表、服务端口或 Python 调度器。

- 每个 Rust 角色进程默认至多两个常驻模型子进程（ASR、Embedding 各一个；最多一个非 CPU 模型）；可通过 `VIDEOROLL_V2_LOCAL_INFERENCE_POOL_SIZE=1` 恢复单进程单模型。相同模型路径、设备及加载参数复用。目录原子替换通过 inode/mtime 使缓存失效，同类模型切换时回收旧进程。设备探测不占常驻槽位。运行期间不支持原地修改模型文件。
- 有界长度帧 JSON 协议，输入 256 KiB、输出 12 MiB，stdout 原生日志重定向。串行交换，等待准入和执行均计入调用期限。超时、取消、退出、损坏帧或模型错误均淘汰当前子进程，不自动重复推理；上层现有重试机制仍生效。
- Linux GPU/非明确 CPU 设备以共享本地 flock 文件实施跨角色准入，驻留期间持锁；明确 CPU 不持 GPU 锁。锁按归一化设备名生成 SHA-256 短标识，并按 slot 编号拆分；默认每设备 1 个 slot，可通过 `VIDEOROLL_V2_LOCAL_INFERENCE_GPU_SLOTS` 配置 1–8，Python bridge 接收同名值 `VIDEOROLL_LOCAL_GPU_SLOTS`，非阻塞轮询空闲 slot。默认共享存储根下 `.local-inference-gpu.lock` 为锁文件基名，可显式指定，所有角色必须使用同一路径、同一服务用户和同一底层本地文件系统。文件禁止符号链接、硬链接、不安全权限；不要在运行时删除锁文件。直接执行旧单次桥接脚本是诊断方式，不受此准入管理。
- CPU 空闲默认 300 秒自动卸载，可通过 `VIDEOROLL_V2_LOCAL_INFERENCE_IDLE_SECONDS` 配置 1–600 秒。GPU 空闲默认取 CPU 空闲时间与 30 秒的较小值，可独立配置 `VIDEOROLL_V2_LOCAL_INFERENCE_GPU_IDLE_SECONDS`（1–600 秒）。子进程 kill-on-drop 与 Linux parent-death signal；systemd 原有 KillMode=control-group 保留。未验证任意第三方派生孙进程的强制清理、整机断电恢复和不同外部应用的 GPU 并发。并不保证跨进程 FIFO、公平性或自动 VRAM 配额；长批次连续使用可能使其他 GPU 调用等待超时。CPU 仍按角色独立运行，没有全机 CPU 模型数限制。
- 有界池允许 CPU ASR 与 CPU Embedding、GPU ASR 与 CPU Embedding 等组合复用；两个 GPU 模型仍需交替淘汰，避免同角色等待自身持有的锁。调用仍按角色串行；超时/取消只淘汰当前子进程，空闲模型可在其他调用期间独立回收。默认不扩大单 GPU 驻留数量。

## 验证记录（2026-11-25）

真实模型同输入冷/热调用（秒，单组测量，不是吞吐量基准）：OpenVINO Whisper large-v3 GPU 54.256/5.222；faster-whisper large-v3 CPU INT8 四线程 35.334/12.677；OpenVINO CPU BGE 512 维 0.9142/0.00737。相同子进程和输出一致性通过；独立 Rust 进程争用 GPU 锁超时，切换 CPU 后释放锁。

- 模拟桥接生命周期测试通过：并发序列化、模型目录替换、超时、调用取消、异常退出、超大/坏 JSON、显式错误与空闲回收。
- 原生产非敏感 ASR 配置隔离测试通过：真实 Rust Worker、GPU Whisper 时间戳、原 assets SRT；真实 BGE 写入、维数校验和重建通过。
- 原 RAG 专项通过：在线模拟及真实 BGE 检索。Rust 全目标 check 通过，库测试 180/180。上一轮完整 smoke 已通过；本轮只重新执行模型相关专项，不将其记作新的 217 全量验收。
- 日志：共享 target 下 `model-runtime-mock.log`、`model-runtime-real.log`、`model-runtime-worker.log`、`model-runtime-rag.log`、`model-runtime-lib.log`。无残留测试 PG 卷或推理子进程。

本轮没有写入生产数据库、切换生产配置、启动 VideoRoll、使用生产 API 密钥或真实投稿。没有测试 SentenceTransformers、长视频端到端吞吐、多个并行真实 Worker 的长期压力场景。


## External Whisper 请求与并发

Rust `external-whisper` 请求复用原 `subtitle.asr` 设置，向 OpenAI-compatible `/v1/audio/transcriptions` 发送 `verbose_json`、语言、temperature、beam_size、batch_size、word_timestamps、VAD 阈值/静音/填充、previous-text 条件及 no-speech/log-prob/compression 阈值。仅在扩展字段遭遇 HTTP 400/422 时重试一次 portable OpenAI 字段；连接错误最多 4 次指数退避，所有请求 attempt 仍经过 `ai_usage` 记录。

`external_whisper_max_concurrency`（1–8，默认 1）使用独立 PostgreSQL session advisory slot（直接连接真实 PostgreSQL，不支持事务池模式的代理）。等待时关闭未获得锁的独立连接，获得 slot 后不保留空闲事务；本地调用完成、超时、取消或连接断开后关闭专用连接释放锁，不返回业务连接池。请求期间每 10 秒检查锁连接与任务租约，连接故障终止本地请求。ASR word timestamps 会按 12 秒/120 字默认上限重新组合，并裁剪到音频 chunk 时长。

## 双模型常驻池专项验收（2026-11-26）

- Mock 子进程：原生命周期回归与双模型池专项均通过；验证同类替换、ASR/Embedding 同时驻留、设备探测不占驻留槽、独立错误处理及空闲回收。
- GPU 锁文件专项：3 项通过，包括 OpenVINO GPU 与 openvino:GPU、CUDA 与 CUDA:0 的归一化。
- 真实离线语音合成素材，原本地模型只读加载，未访问生产数据库：OpenVINO GPU ASR 冷/热 51.640/1.311 秒；OpenVINO CPU BGE 冷/热 2.214/0.0094 秒；faster-whisper CPU INT8 冷/热 34.452/12.897 秒。验证 GPU ASR 与 CPU Embedding 同时驻留、交替调用复用原进程及相同输出，另一个 Rust 进程争用 GPU 准入槽位时超时，切换 CPU ASR 后释放原 GPU 锁、保留 CPU Embedding。真实专项共 104.81 秒，测试通过。
- 真实专项使用 GPU 空闲 120 秒防止长冷启动测量干扰生命周期断言；生产默认 GPU 空闲 30 秒，CPU 空闲 300 秒。未验证多 GPU 同时常驻或跨角色长期公平性，未提升默认 GPU admission slots。


## External Whisper 并发锁专项验收（2026-11-26）

使用全新 PostgreSQL 18 容器（idle_in_transaction_session_timeout=100ms）和 loopback 模拟 Whisper HTTP 服务。4 个请求任务、cap=2，实际最大同时 HTTP 调用数为 2；模拟 422 拒绝扩展字段后 4 次 portable 重试全部成功，8 条 AI 用量记录齐全。实际数据库会话锁跨越空闲事务超时仍有效，终止锁连接可检测失败，取消持锁任务可重新获取锁。容器及专属卷已删除。

可复现：`rust/scripts/test-external-whisper-isolated.sh`（要求本地 Docker 及已存在的 pgvector/pgvector:pg18 镜像）。不访问生产数据或真实 Whisper 服务。请求被上游接受后，即使本地连接中断也无法保证第三方立即终止推理；Python 旧 Redis 准入与 Rust PostgreSQL 准入不共用，不能把两套调度器同时运行视为统一并发限制。
