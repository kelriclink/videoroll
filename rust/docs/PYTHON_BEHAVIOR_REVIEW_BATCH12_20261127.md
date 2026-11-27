# 第十二批：字幕可读性、质量报告、资源监控

基线 a3839be。继续使用 PYTHON_SOURCE_INVENTORY_20261127.csv 追踪第一方 Python 功能，未宣称全仓语义审查完成。

## 本轮原代码阅读与应用

- subtitle_service/subtitle_readability.py 全部实现、worker.py:1751–1800：自适应中英日/韩宽度、CPS、短字幕延时、标点断行、双语显示、长字幕按宽度加权拆分。Rust media/readability.rs，使用从 Python unicodedata 生成的静态 Unicode 宽度及标点区间，不新增运行时 Python 依赖。原 source/translated 快照仍为一对一，不把展示字幕写回翻译索引；仅 SRT/ASS 使用优化后的 cues。选择性重译基于原始快照重新计算展示，并同步更新快照质量记录。过短长字幕维持一个显示事件，不能保证所有行都低于理想宽度。双语保留 secondary_text。
- subtitle_service/subtitle_quality.py 全部实现、translation_context.py scene profile、原质量查询接口：Rust 生成版本1评分、CPS 分位数、时长、行宽、重叠、术语和相同源译文计数、场景及上下文统计，issues 最多 200。用已有 operation_inbox.result_json 保存报告，原 native/legacy job 按时间选择，历史 Python quality_key 读取保持不变；native inline report 不伪造物理 report key。术语匹配复用 Rust mentions，非 ASCII Unicode casefold 并非逐字符等价。
- 同一 job 在 SRT 已登记但工作流结算前崩溃时，复用同一报告，核对 segments/SRT/ASS 哈希与租约。新普通字幕先登记 ASS，最后登记 SRT；旧版历史 SRT 复用不假造质量报告。仍需全面验证选择性重译的真实翻译模型/上下文和所有异常写入组合。
- utils/resources.py、utils/cpu.py、utils/intel_gpu.py、orchestrator_api/services/system_service.py：CPU /proc/stat 差分（guest 不重复计数）、cgroup v1/v2 CPU/内存限制及真实 Intel GPU PMU 采样。原 Python 依赖 sysfs busy，实际 link Arc i915 没有该计数器；改用已安装 /usr/bin/intel_gpu_top 流式 JSON，最多512KiB/2s，第二个完整样本，仅返回引擎使用率，无进程明细。按原 auto_profile.use_intel_gpu 开关和 INTEL_GPU_RENDER_DEVICE，HTTP/WS 共用进程内3s缓存；PMU 工具或权限不可用时明确 unsupported，不伪造0%。当前只在 link 的实际 Intel i915 设备采样，其他驱动/非Linux未验收。

## 验收

- 260 Rust 库测试通过，含真实 host CPU 及 Intel PMU（CPU 20.574%，40 cores，Arc 8086:56a5，5 engines 空闲）。这是采样功能验收，不是负载性能测试。
- Python 实际函数生成的 10 组差分 fixture：自适应 profile、展示文本、时刻误差<=1ms、拆分/延长计数、质量分数；通过。Python 参考测试 31/31。
- 真实隔离 PostgreSQL + FFmpeg 合成视频 + target captions：2个原始对齐 cues -> 多个展示 cues，文件时间与行数有效，原 source/translated 不变；同 job 重试保持报告，scheduler 持久化，HTTP quality endpoint 原样读出。通过。测试直接提供字幕，不使用真实 ASR/LLM，也不把未翻译的字幕标记为已翻译。选择性重译实际模型专项仍待验收。
- 日志 /tmp/vr-b12-final-lib.log、/tmp/vr-b12-native.log、/tmp/vr-b12-python2.log；参考样例 /tmp/vr-b12-readability-fixture.json，生成器 rust/tests/readability_fixture.py。
- 初次 Python 测试 core venv 无 pytest，复用 dubbing-download 中的 pytest 后通过；初次库回归257项，最终260项，PG单独实测。
- 不部署、不推送、不改生产数据库。第十批 Release 60d8c41 尚不包含第十一、十二批。

## 后续审查

P0 WebSocket 提交顺序/删除通知/重连、源文件丢失恢复与历史投稿保留；P1 队列统计与租约同步、安全认证/可信反代、社交投稿账号轮换。继续按原文件清单阅读实际实现及测试，不用 AST 扫描冒充全量等价。
