# Python 行为等价专项第二批记录

第一批索引：PYTHON_BEHAVIOR_REVIEW_20261126.md。全量索引覆盖201文件、65413行、2710个定义；目前不是完整逐行语义签收。以下逐条记录实际实现对照，不把 AST 数量当成完成度。

## 已通过测试并集成

- ASR-01：3551806，合并46196fd；重叠语句、真实重复、混合词/句时间戳、质量指标、顶层words、尾部窗口；Groq 45s/5s仅窗口已恢复，限流/VAD/冷却仍未迁移。为避免旧缓存污染识别语义，ASR checkpoint revision 3。
- BILI-01：1f2a5f4，合并46196fd；原upload_workers实际生效，小文件顺序，大文件有界并行，单次上传主缓冲区128MiB上限（不含HTTP栈等开销），只重试安全PUT，按partNumber排序、总字节校验、原直接投稿进度。独立HTTP fixture验证peak=3、503后只重试失败分片、全部字节一致。跨任务全局节流仍需BILI-02，未真实投稿；自动路径无原publish_jobs行时仍需前端进度投影验证。
- TR-01：57341e4，合并41017ed；通用OpenAI请求预算检查、缺失索引补译、partial补译保留、动态缩批、主流程指纹化局部进度文件、硬术语块索引重映射、最终完整质量检查。真实隔离PG+HTTP fixture验证部分结果恢复、503缩批、只补缺失行、上下文预检。选择性重译路径已复用缩批，但不具有新主流程独立partial文件；真实tokenizer未接入，估算遵循旧版保守策略。

测试记录：首次lib 222/222；翻译专项实际隔离测试1/1、随后lib 224/224（未设置专项环境时相关真实测试函数直接返回，不能重复算成真实数据库验收）。没有生产写入或真实平台调用。

## 新增审计事项

| 编号 | 优先级 | Python 证据 | Rust 证据 | 审查结论 |
|---|---|---|---|---|
| DUB-01 | P0 | apps/dubbing/youdub_native.py:153–289 | media/dubbing.rs:77–270 | 原host独占GPU锁、等待GPU进度、每5秒状态轮询/控制、running状态interrupt、resident进程取消未见完整对应。Rust kill_on_drop只保证直接子进程，不能等同于杀死resident任务；status任意错误直接create，需仅在明确not-found时create。 |
| DUB-02 | P0 | apps/dubbing/youdub_job.py:36–120及后续音频handoff；youdub_native.py:175–289 | media/dubbing.rs:111–270 | 旧版新任务audio_only.flag、语音及混音/SRT成果交接RenderWorker；Rust目前直接导入output_video。需校验原音频轨、对齐字幕、实际render模式、P2时长；已完成dest快速返回可能绕过dubbing_run来源证明写入，恢复须核对并重建同一receipt。 |
| META-01 | P1 | apps/publish_meta_rules.py:60–174；publish_meta_draft.py:185–241 | publish_meta.rs:219–359 | 源标题保留博主后缀、标题长度为后缀预留、来源块放在简介开头且去重、按双字符单位裁剪、publish_enable_reprint配置；Rust source draft目前截取title 80、desc后附来源且仅空desc时处理、固定转载。需明确用户覆盖的优先级并验证旧metadata_json/info路径。 |
| META-02 | P1 | apps/publish_meta_draft.py:48–104 | publish_meta.rs:title_for/build_draft、subtitle translation | 旧版无中文且开启title翻译时调用所选模型（Index/OpenAI），已有译名不重译，四次重试；Rust目前读取保存标题，需核对自动生成未译标题和缺失翻译凭据的真实行为，不应默默丢失标题翻译设置。 |
| META-03 | P1 | apps/publish_request_builder.py:60–114 | publish_meta.rs:351–357；bilibili_account_v1.rs:569–637 | Rust已有AI→平台建议→泛化标签路径；source draft提前填视频分享是否跳过推荐需端到端核查，不能直接认定整个标签生成缺失。 |
| SAU-01 | P1 | apps/social_publisher/worker.py:177–255,372–410；runtime.py:19–24 | sau_worker_native_v1.rs:114–219,367–545 | 旧版按账号锁，Rust全局串行准入更保守，有原账号/登录状态/lease/unknown保护；不同账号并行吞吐属于有意设计差异待确认，不应为性能放宽未知投稿隔离。 |
| WF-01 | P0 | workflows/video_pipeline.py:137–212,339–635；pipeline_status_sync.py:79–417；execution_truth.py | scheduler.rs:649–1120,1330–1490、task_ownership.rs、outbox_dispatcher.rs | 当前已核查租约、终态、未知、恢复入口存在；Hatchet失联状态应转换为Rust持久化状态而不是复制旧外部探测。仍需逐项故障注入及队列/手动恢复行为测试。 |
| WS-01 | P1 | realtime.py:262–447 | realtime.rs event_batch/serve | 原ORM after_commit按实体发任务、字幕、渲染、配音、稿件、批次、资产、账号事件且回滚丢弃；Rust原生事件流需验证相同更新和删除信号、断线重连及上传进度前端刷新。不能只凭WebSocket接口存在宣称等价。 |
| EGRESS-01 | P1 | apps/egress_gateway/client.py:221–312,324–470 | egress_gateway_v1.rs:133–408,421–477,537–570 | 已找到DNS固定、私有/混合DNS禁止、redirect、body限制及总超时对应；继续核对Retry-After、响应编码、代理、并发限制及全部deadline语义。不得降低Rust SSRF防护。 |
| STORE-01 | P1 | storage/filesystem.py:86–201,241–322；utils/workdir_maintenance.py:52–224 | storage.rs、maintenance_workdir_v1.rs、maintenance_terminal_storage_v1.rs | 已有原引用/清理路径；待核对原始partial目录过期、范围读取、原子落盘、跨设备搬运与生产原路径的空间统计；此前真实DDL资源清理测试夹具仍需补priority等非空列，不能当作已通过。 |

## 待完成的原清单

BILI-02/03/04（队列节流、合集、最终元数据与确定拒绝修正）；ASR-02/03/04（Groq限流、共享429冷却、Silero及音频边界）；YT-01/02/03/04（下载后端降级、脱敏诊断、源完整性、元信息）；RENDER-01/02/03（本轮在独立工作树编写成品缓存/回执恢复，尚待实际编译；硬件隔离仍待）；RAG-01/02（跨任务缓存/熔断与检索证据差分）；TR-02/03、DICT、SUBAPI、WORKER；以及剩余Python定义的语义核查。持续更新具体证据，不应将这些开放项视作完成。
