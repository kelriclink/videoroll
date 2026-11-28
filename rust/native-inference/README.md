# Rust 原生本地推理适配器（可选）

独立 Rust crate，与主程序的长度前缀 JSON 协议兼容。Worker 保留现有租约、检查点、GPU 互斥、子进程池与字幕质量控制。原生适配器缓存每个会话的模型并使用相同的设备锁；支持 OpenVINO Whisper、原 Silero V6 ONNX、OpenVINO IR Embedding 和设备枚举。未支持 CTranslate2/Faster-Whisper 和 SentenceTransformer 非 OpenVINO 后端，仍由原适配器处理。

- OpenVINO / GenAI 2026.4.1：Intel 官方 GenAI Ubuntu24 x86_64 原生归档，解压到 `/opt/videoroll/runtime/openvino-2026.4.1`；官方 SHA-256 `b8ba7b6e5ae7899e63e5e3c7f9d022ad8a4207247521e46c507f6f3ef23ea2c5`。
- ORT 1.30.0 / Silero V6：独立目录 `/opt/videoroll/runtime/silero-v6-ort-1.30.0`，包含 `libonnxruntime.so`（指向 1.30.0）、`libonnxruntime_providers_shared.so` 和 `silero_vad_v6.onnx`；权重和原生库可从已验证的原 faster-whisper/onnxruntime 分发包提取，无需运行 Python。VAD 分段算法依据现有 faster-whisper (MIT) 行为和 Silero VAD (MIT)。
- 编译：`cargo build --release --locked --manifest-path rust/native-inference/Cargo.toml`，将 `videoroll-native-inference` 安装为 root-owned 0755 `/opt/videoroll/bin/videoroll-native-inference`。
- 启用：`VIDEOROLL_V2_LOCAL_INFERENCE_NATIVE=/opt/videoroll/bin/videoroll-native-inference`。原生运行库路径通过 `VIDEOROLL_V2_OPENVINO_RUNTIME` 和 `VIDEOROLL_V2_SILERO_RUNTIME` 可显式指定。未设 native 路径时行为不变。新原生后端加入 ASR 指纹，避免混用旧后端的部分检查点。
- 本地测试：`cargo test --locked --manifest-path rust/native-inference/Cargo.toml`。真实硬件及不同语言、长音频、断点恢复需要单独对比。不要删除仍用于其他本地引擎的 Python 环境。

## link 实机初验（2026-11-28）

- 同一 7.04 秒合成英语语音，A380/Whisper Large V3 FP16：旧 Python 27.160 s、Rust 单次 28.178 s，峰值独立显存 3822.4 / 3822.3 MiB；识别文本一致。常驻 Rust 第二次调用 1.790 s，返回与旧适配器 JSON 一致。首个常驻请求包含生产 GPU 锁等待，不计为纯推理耗时。
- 7.04 秒和含 3 秒静音的 17.08 秒样本，ASR VAD 与 timing VAD 共四组时间区间完全一致。
- CPU BAAI/bge-small-zh-v1.5，512 维，测试句向量逐元素相等（余弦 1.0）。当前生产配置 Qwen3-Embedding-8B 未在本地模型目录发现、RAG 禁用；不声称已验收该模型。
- 以上仅是有限输入的等价性初验，不代表多语言、长视频或全部模型的完整质量测试。保留原适配器作为尚未原生化的引擎路径；不得仅依据这些结果删除整个 subtitle 环境。
