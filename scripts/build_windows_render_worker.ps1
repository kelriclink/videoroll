$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$Vendor = Join-Path $RepoRoot "build\windows-render-worker\vendor"
$FfmpegRoot = Join-Path $Vendor "ffmpeg"
$FfmpegZip = Join-Path $Vendor "ffmpeg.zip"
New-Item -ItemType Directory -Force -Path $FfmpegRoot | Out-Null

Write-Host "Downloading FFmpeg Windows build..."
Invoke-WebRequest -Uri "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile $FfmpegZip

$Extracted = Join-Path $Vendor "ffmpeg-extracted"
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Extracted
Expand-Archive -Path $FfmpegZip -DestinationPath $Extracted

$FfmpegExe = Get-ChildItem -Path $Extracted -Filter ffmpeg.exe -Recurse | Select-Object -First 1
$FfprobeExe = Get-ChildItem -Path $Extracted -Filter ffprobe.exe -Recurse | Select-Object -First 1
if (-not $FfmpegExe) { throw "ffmpeg.exe not found in downloaded archive" }
if (-not $FfprobeExe) { throw "ffprobe.exe not found in downloaded archive" }

Copy-Item $FfmpegExe.FullName (Join-Path $FfmpegRoot "ffmpeg.exe") -Force
Copy-Item $FfprobeExe.FullName (Join-Path $FfmpegRoot "ffprobe.exe") -Force

$EncoderText = & (Join-Path $FfmpegRoot "ffmpeg.exe") -hide_banner -encoders 2>&1 | Out-String
$FilterText = & (Join-Path $FfmpegRoot "ffmpeg.exe") -hide_banner -filters 2>&1 | Out-String
if ($EncoderText -notmatch "h264_nvenc") { throw "bundled FFmpeg is missing h264_nvenc" }
if ($EncoderText -notmatch "av1_nvenc") { throw "bundled FFmpeg is missing av1_nvenc" }
if ($FilterText -notmatch "\bass\b") { throw "bundled FFmpeg is missing the ass subtitle filter" }

Write-Host "Building VideoRollRenderWorker.exe..."
$PyInstallerArgs = @(
  "--noconfirm",
  "--clean",
  "--onedir",
  "--windowed",
  "--contents-directory", ".",
  "--name", "VideoRollRenderWorker",
  "--paths", "src",
  "--add-binary", "$FfmpegRoot\ffmpeg.exe;.",
  "--add-binary", "$FfmpegRoot\ffprobe.exe;.",
  "src\videoroll\apps\render_worker\windows_client.py"
)
python -m PyInstaller @PyInstallerArgs

$Dist = Join-Path $RepoRoot "dist\VideoRollRenderWorker"
$Readme = @"
VideoRoll Windows Render Worker
================================

1. 双击 VideoRollRenderWorker.exe
2. 填写 VideoRoll 主站 API 地址，例如 https://example.com/api
3. 在 VideoRoll 的“渲染管理”页面生成一次性 vre_* 配对 Token
4. 粘贴 Token，点击“检测硬件”，确认 RTX/NVIDIA GPU 和 NVENC 编码器可见
5. 点击“启动节点”

首次配对后 Worker 凭据保存在：
%LOCALAPPDATA%\VideoRoll\RenderWorker\credential.json

日志保存在：
%LOCALAPPDATA%\VideoRoll\RenderWorker\render-worker.log

客户端自带 FFmpeg。NVIDIA GPU 需要已安装官方驱动（nvidia-smi 可用）。
"@
Set-Content -Path (Join-Path $Dist "README-Windows.txt") -Value $Readme -Encoding UTF8

$ZipPath = Join-Path $RepoRoot "dist\VideoRollRenderWorker-windows-x64.zip"
Remove-Item -Force -ErrorAction SilentlyContinue $ZipPath
Compress-Archive -Path "$Dist\*" -DestinationPath $ZipPath -CompressionLevel Optimal
Write-Host "Built: $ZipPath"
