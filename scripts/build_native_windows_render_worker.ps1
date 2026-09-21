$ErrorActionPreference = "Stop"

$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $RepoRoot

$Manifest = Join-Path $RepoRoot "native\windows-render-worker\Cargo.toml"
$Stage = Join-Path $RepoRoot "dist\native-windows-render-worker"
$Vendor = Join-Path $RepoRoot "build\native-windows-render-worker"
$FfmpegZip = Join-Path $Vendor "ffmpeg.zip"
$FfmpegExtract = Join-Path $Vendor "ffmpeg"

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $Stage
New-Item -ItemType Directory -Force -Path (Join-Path $Stage "bin") | Out-Null
New-Item -ItemType Directory -Force -Path $Vendor | Out-Null

Write-Host "Building native Rust worker..."
$env:RUSTFLAGS = "-C target-feature=+crt-static"
cargo build --release --manifest-path $Manifest

$WorkerExe = Join-Path $RepoRoot "native\windows-render-worker\target\release\VideoRollRenderWorker.exe"
if (-not (Test-Path $WorkerExe)) {
  throw "Rust worker executable missing: $WorkerExe"
}
Copy-Item $WorkerExe (Join-Path $Stage "VideoRollRenderWorker.exe") -Force

Write-Host "Downloading static FFmpeg Windows build..."
Invoke-WebRequest -Uri "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip" -OutFile $FfmpegZip

Remove-Item -Recurse -Force -ErrorAction SilentlyContinue $FfmpegExtract
Expand-Archive -Path $FfmpegZip -DestinationPath $FfmpegExtract
$FfmpegExe = Get-ChildItem -Path $FfmpegExtract -Filter ffmpeg.exe -Recurse | Select-Object -First 1
$FfprobeExe = Get-ChildItem -Path $FfmpegExtract -Filter ffprobe.exe -Recurse | Select-Object -First 1
if (-not $FfmpegExe) { throw "ffmpeg.exe missing from downloaded build" }
if (-not $FfprobeExe) { throw "ffprobe.exe missing from downloaded build" }
Copy-Item $FfmpegExe.FullName (Join-Path $Stage "bin\ffmpeg.exe") -Force
Copy-Item $FfprobeExe.FullName (Join-Path $Stage "bin\ffprobe.exe") -Force

$Encoders = & (Join-Path $Stage "bin\ffmpeg.exe") -hide_banner -encoders 2>&1 | Out-String
$Filters = & (Join-Path $Stage "bin\ffmpeg.exe") -hide_banner -filters 2>&1 | Out-String
foreach ($Required in @("h264_qsv", "hevc_qsv", "h264_nvenc", "hevc_nvenc", "av1_nvenc")) {
  if ($Encoders -notmatch [regex]::Escape($Required)) {
    throw "bundled FFmpeg is missing $Required"
  }
}
if ($Filters -notmatch "\bass\b") {
  throw "bundled FFmpeg is missing the ASS subtitle filter"
}

$Readme = @"
VideoRoll Native Render Worker
==============================

All runtime files live under this installation directory.

Layout:
  VideoRollRenderWorker.exe
  bin\ffmpeg.exe
  bin\ffprobe.exe
  config\config.json
  config\credential.json
  logs\render-worker.log
  cache\
  work\

First pairing:
1. Open VideoRoll Render Management.
2. Generate a one-time vre_* enrollment token.
3. Start VideoRoll Render Worker.
4. Enter the external API root, for example https://video.example.com/api.
5. Paste the token, scan hardware, then start the worker.

The client probes Intel QSV and NVIDIA NVENC encoders by actually encoding a
64x64 one-frame sample before reporting capabilities to the coordinator.
"@
Set-Content -Path (Join-Path $Stage "README.txt") -Value $Readme -Encoding UTF8

$PortableZip = Join-Path $RepoRoot "dist\VideoRollRenderWorker-Native-Portable-x64.zip"
Remove-Item -Force -ErrorAction SilentlyContinue $PortableZip
Compress-Archive -Path "$Stage\*" -DestinationPath $PortableZip -CompressionLevel Optimal

$Iscc = Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
if (-not (Test-Path $Iscc)) {
  Write-Host "Installing Inno Setup..."
  choco install innosetup --yes --no-progress
}
if (-not (Test-Path $Iscc)) {
  throw "ISCC.exe not found after Inno Setup installation"
}

Write-Host "Building installer..."
& $Iscc "native\windows-render-worker\installer\VideoRollRenderWorker.iss"
if ($LASTEXITCODE -ne 0) {
  throw "Inno Setup build failed with exit code $LASTEXITCODE"
}

$Installer = Join-Path $RepoRoot "dist\VideoRollRenderWorker-Setup-x64.exe"
if (-not (Test-Path $Installer)) {
  throw "installer missing: $Installer"
}

Write-Host "Built native worker:"
Get-Item (Join-Path $Stage "VideoRollRenderWorker.exe"), $PortableZip, $Installer |
  Select-Object FullName, Length
