# Rust 本地推理与隔离验收

Rust 控制 ASR、Embedding 请求、任务状态、字幕资产及原 PostgreSQL 知识库；本地模型通过受限单次 JSON 子进程调用现有第三方运行库，不启动旧 Python 业务服务。模型路径必须位于配置根目录，默认禁止模型联网下载；请求有超时及输出上限。

## 实际验证（2026-11-24）

- 原生产 `subtitle.asr` 的 OpenVINO Whisper large-v3 FP16 GPU 配置：合成英文语音真实推理成功；隔离 Rust worker 生成 SRT 并登记原资产表。
- faster-whisper large-v3 CPU INT8：独立模型调用成功并返回时间戳。
- 本地 BGE-small-zh-v1.5 OpenVINO：512 维归一化向量；Rust API 维度检查、知识库写入和向量重建通过。
- 隔离测试只使用原生产 `subtitle.asr`、`subtitle.translate`、`subtitle.auto_profile` 的白名单非敏感字段；关闭真实翻译、投稿和配音等副作用。生产数据库仅只读查询。原生产在线 Qwen3-Embedding-8B 4096 维配置未被替换；本地 BGE 为单独测试配置。
- 全量 smoke 已通过修正后的 YouTube 原接口列表轮询测试；历史 P2 取消差分仍在所有权保护处返回 409，保留防护，不算通过。
- 217 接口探测按原 service/method/path 去重，独立启动各服务并验证鉴权。请求级探测不等于逐条成功路径与 Python 数据库副作用等价。报告位于 `rust/target/contract-parity-217.json` 和同名 Markdown。

## 尚存限制

- 当前每次调用加载模型，尚未实现常驻模型池或跨进程 GPU 限流；长视频性能及模型版本指纹仍需验收。
- SentenceTransformers 分支已接入，现有推理环境未安装相应依赖，未真实运行；实际验证的是 OpenVINO Embedding。Cloudflare ASR、原知识库高级 RAG 检索消费者仍未完全迁移。
- 未验证在线 Qwen Embedding 真实端点、全部生产配置、真实投稿或历史数据库接管。生产 PostgreSQL collation 记录版本 2.41 与容器 2.36 不匹配，需另行备份、验证重建索引及升级方案；此次未修改。

### 本轮 217 接口独立探测结果

217/217 已探测：107 正向、108 拒绝路径、1 投稿隔离阻断、1 异常（独立字幕服务远程模型列表 HTTP 502）。22 个 404 需区分缺失测试夹具和路由缺口，其中 19 个来自独立服务；不将拒绝响应记为成功路径等价。报告完整签收为 0。全量 smoke 与单独探测均未全绿。
