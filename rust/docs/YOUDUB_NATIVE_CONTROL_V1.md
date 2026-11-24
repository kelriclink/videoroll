# YouDub VoxCPM2 原生控制 PUT — 2026-11-24

迁移原 FastAPI PUT /settings/dubbing/native，使用相同请求和响应形状：
{ "enabled": boolean, "resident": boolean | null }；enabled 必须提供，
resident 省略或为 null 时沿用旧值，禁用时强制取消驻留。
响应与已有 GET /settings/dubbing/native 一致。

实现完全由 Rust 维护控制流程，YouDub 实际模型与媒体处理仍由现有
scripts/native/youdub_bridge.py Sidecar 负责，不重写 TTS、拆轨、混音等模块。
与 Python 同步使用 videoroll-control.json 和 .videoroll-control.lock，
原子替换 JSON，不创建新的数据库业务表。

行为保留及防护：
- 启用前检查 upstream Python 环境和 VoxCPM2 模型文件（非真实加载模型）；
  未安装返回冲突。禁用/修改驻留模式前同时检查原 dubbing_jobs 与
  Rust 原生 operation_inbox 的活动配音记录。
- 常规按需模式只改变控制状态，不占据 GPU；开启驻留时通过 nvidia-smi
  验证至少 12,000 MiB 空闲显存，然后调用已有 YouDub Bridge --serve。
- Linux Unix Socket ping 真正返回 gpu_loaded=true 才将该状态报告为真。
  初次加载期间可以显示 resident=true, gpu_loaded=false，不能伪称已加载。
- PID 必须与原 YouDub Bridge 路径和 --serve 参数匹配，才允许 SIGTERM/
  超时 SIGKILL；非法/被复用 PID 不会被杀死。不覆盖未知来源的活动驻留
  Socket。已有合法进程不重复启动，停用后清理 PID/Socket。
- 复用旧 Python 文件锁；Rust 与 Python 配置调用相互串行。控制进程时
  不会停止正在排队或执行的配音任务。
- VIDEOROLL_V2_YOUDUB_BRIDGE、VIDEOROLL_V2_YOUDUB_PYTHON、
  VIDEOROLL_V2_YOUDUB_RUNTIME 沿用现有 Rust 配置；默认 Bridge 查找
  项目根和 rust/ 的相对路径。

通过独立临时 PostgreSQL、HTTP 和伪造的 VoxCPM2 模型树/Unix Socket/
GPU 查询脚本进行隔离测试。覆盖：无认证、缺模型、配置持久化、Python
flock 互斥、活动任务拒绝切换、显存门槛、驻留进程创建与复用、
实时 GPU 状态、外来 PID 防误杀和安全退出。

没有触发真实 VoxCPM2 加载，也没有实际操作 V100；真实 GPU 模型启动、
多进程滚动切换、生产环境启动策略仍须额外验收。
