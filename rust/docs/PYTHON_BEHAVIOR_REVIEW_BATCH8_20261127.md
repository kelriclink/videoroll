# 第八批：配音渲染独立重试、RAG 物理预算与 ASR 语义分段

基线 d0c39be。先审查 Python 的 youdub_job、translation_rag/原 RAG 实现及 processing.py 中 semantic_regroup；保持原业务表和 API。未执行生产写入、真实投稿、服务重启或部署。

## 实现
- 原配音 retry API 优先识别已经持久交接的 dub_burn_in 执行。仅允许最新 Rust 工作流、明确远程渲染失败、无未确认投稿、无活跃渲染租约或已提交输出时重新排队。验证冻结的六项输入/音频/字幕证据及原工作流存储引用；损坏或缺失证据失败关闭，不回退重新合成。保留模型、工作流、配音 job、渲染 execution ID 与原 render_spec。重置尝试数并使用新 worker fence。并发重复调用只创建一个重排事件。遵守旧手动重试默认不自动投稿的语义。
- RAG：在现有 operation_inbox.result_json 内保存每个字幕操作的物理 HTTP 计数及上限。每次请求/重试先持久预留，缓存命中不增加物理计数，跨连接池原子计数，恢复时不能提高已冻结上限。普通 Web 使用 max_web_searches，Wiki/页面各最多 2 次；不新增业务表。已有逻辑工具预算继续保留；本地预算耗尽不计为服务商故障。此项限制的是应用层 HTTP 派发次数，不保证传输层重传次数。
- 外部 Whisper：依据原 Python 的句末标点、停顿、软/硬长度与短片段合并策略重组词级时间轴，同一响应中跨原模型分段。保留 segment-only 区间及质量分类边界，避免把可疑语音与正常语音混合；不为缺失单词时间戳的内容虚构词时间戳。ASR 检查点版本 4 升至 5。跨长音频请求窗口的全局单词时间线仍需完善。

## 验收
- Rust 库 251/251（初次功能构建）；随后原表 started_at NOT NULL 约束导致配音专项失败，修正为重排队时间后重新编译，专项通过，不修改 DDL。
- 独立 PG/HTTP 专项五组通过：dub-handoff（删除测试模型运行目录后仍可仅重试渲染、损坏音频拒绝、双并发仅一次重排、旧 fence 拒绝、真实 HTTP 音频/ASS/MP4 及回调恢复）、rag-shared（物理预算、跨连接池共享缓存、503/429、并发峰值 4）、external-whisper（Groq/CF HTTP 模拟与 PG 并发/取消）、thinking（现有 SSE 与 Cerebras 回退、用量统计）、collection（已有合集未知回执恢复，保留人工合集）。后四组使用最终 SQL 单行修正前的同批二进制，未受该修正影响。
- 原 Python 参考测试：test_external_whisper_asr.py、test_asr_audio_memory.py、test_translation_rag.py，95 通过，4 跳过。未进行真实模型全量差分。
- 日志：/tmp/vr-b8-lib.log、/tmp/vr-b8-dub-handoff2.log、/tmp/vr-b8-rag-shared.log、/tmp/vr-b8-external-whisper.log、/tmp/vr-b8-thinking.log、/tmp/vr-b8-collection.log、/tmp/vr-b8-python.log。

## 剩余
- Thinking 实时前端增量事件仍未实现；本轮仅回归现有流式响应解码与兼容回退。
- 真实 B 站合集、VoxCPM2/Demucs、Windows GPU RenderWorker、长音频全局分段、最终 release 与原生产配置切换待验收。检查时 V100 已占用 25,899/32,768 MiB，未抢占正在运行的模型。
