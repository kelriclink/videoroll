# 本地模型常驻与并发保护

基线 91824c3。Rust 仍掌握任务、配置、超时、存储和数据库；Python 仅作为第三方模型运行适配器。没有新增业务表、服务端口或 Python 调度器。

- 每个 Rust 角色进程至多一个常驻模型子进程；相同模型路径、设备及加载参数复用。目录原子替换通过 inode/mtime 使缓存失效，切换模型或设备时回收旧进程。运行期间不支持原地修改模型文件。
- 有界长度帧 JSON 协议，输入 256 KiB、输出 12 MiB，stdout 原生日志重定向。串行交换，等待准入和执行均计入调用期限。超时、取消、退出、损坏帧或模型错误均淘汰当前子进程，不自动重复推理；上层现有重试机制仍生效。
- Linux GPU/非明确 CPU 设备以共享本地 flock 文件实施跨角色排他准入，驻留期间持锁；明确 CPU 不持 GPU 锁。默认共享存储根下 `.local-inference-gpu.lock`，可显式指定，所有角色必须同一路径且同一服务用户。文件禁止符号链接、硬链接、不安全权限；不要在运行时删除锁文件。直接执行旧单次桥接脚本是诊断方式，不受此准入管理。
- 空闲 30 秒自动卸载，可配置 1–600 秒。子进程 kill-on-drop 与 Linux parent-death signal；systemd 原有 KillMode=control-group 保留。未验证任意第三方派生孙进程的强制清理、整机断电恢复和不同外部应用的 GPU 并发。并不保证跨进程 FIFO、公平性或自动 VRAM 配额；长批次连续使用可能使其他 GPU 调用等待超时。CPU 仍按角色独立运行，没有全机 CPU 模型数限制。
- 单进程单模型是保守显存/内存策略；ASR 与 Embedding 交替时可能反复切换加载。未来可按设备与模型配置扩展有界池。

## 验证记录（2026-11-25）

真实模型同输入冷/热调用（秒，单组测量，不是吞吐量基准）：OpenVINO Whisper large-v3 GPU 54.256/5.222；faster-whisper large-v3 CPU INT8 四线程 35.334/12.677；OpenVINO CPU BGE 512 维 0.9142/0.00737。相同子进程和输出一致性通过；独立 Rust 进程争用 GPU 锁超时，切换 CPU 后释放锁。

- 模拟桥接生命周期测试通过：并发序列化、模型目录替换、超时、调用取消、异常退出、超大/坏 JSON、显式错误与空闲回收。
- 原生产非敏感 ASR 配置隔离测试通过：真实 Rust Worker、GPU Whisper 时间戳、原 assets SRT；真实 BGE 写入、维数校验和重建通过。
- 原 RAG 专项通过：在线模拟及真实 BGE 检索。Rust 全目标 check 通过，库测试 180/180。上一轮完整 smoke 已通过；本轮只重新执行模型相关专项，不将其记作新的 217 全量验收。
- 日志：共享 target 下 `model-runtime-mock.log`、`model-runtime-real.log`、`model-runtime-worker.log`、`model-runtime-rag.log`、`model-runtime-lib.log`。无残留测试 PG 卷或推理子进程。

本轮没有写入生产数据库、切换生产配置、启动 VideoRoll、使用生产 API 密钥或真实投稿。没有测试 SentenceTransformers、长视频端到端吞吐、多个并行真实 Worker 的长期压力场景。
