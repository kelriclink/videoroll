# VideoRoll Windows Render Worker

Windows 节点复用 VideoRoll 的 Render Worker 协议和 Node Local Scheduler，不单独维护第二套渲染协议。

首版目标是 Windows 10/11 x64，Intel 核显/QSV 与 NVIDIA/NVENC 都是一等渲染设备。客户端通过 Windows VideoController + 实际 QSV 编码探测识别 Intel 核显，通过 nvidia-smi 枚举 NVIDIA 显卡，并用一个 64x64 单帧硬件编码预检确认每张卡真正支持的编码器。Coordinator 只把 RenderJob 分配到 Node；具体 GPU 仍由 Windows Node 自己选择。

客户端采用便携式目录而不是安装器。VideoRollRenderWorker.exe 提供服务器、一次性 enrollment token、节点名称和最大并发配置，以及硬件扫描、启动/停止接单和日志查看。首次配对成功后，长期 Worker credential 保存在 %LOCALAPPDATA%\VideoRoll\RenderWorker\credential.json，一次性 token 不写入配置文件。

构建由 .github/workflows/build-windows-render-worker.yml 在 Windows runner 上执行。产物为 VideoRollRenderWorker-windows-x64.zip，其中包含客户端 EXE、FFmpeg 和 FFprobe。

## 使用

1. 在 VideoRoll 渲染管理页面生成一次性 vre_* enrollment token。
2. 解压 Windows 客户端压缩包。
3. 双击 VideoRollRenderWorker.exe。
4. 服务器填写外部 API 根路径，例如 https://video.example.com/api。
5. 粘贴 vre_* token，点击“检测硬件”。
6. 确认目标 Intel QSV / NVIDIA NVENC 设备及编码器出现后，点击“启动节点”。

重新配对会删除本机长期 Worker credential，需要在服务器生成新的 enrollment token。
