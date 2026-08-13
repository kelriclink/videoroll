# VideoRoll YouTube Submit

适用于 Chrome、Edge 等 Manifest V3 浏览器。在 YouTube 视频页、播放器或缩略图链接上右键，即可通过 VideoRoll Remote API 创建自动模式任务。

## 安装

1. 在 VideoRoll Web 的“设置 → API”中设置 Remote API Token，并记下接口地址。
2. 运行 `./scripts/build_browser_extension.sh`，或直接使用本目录作为未打包扩展。
3. Chrome 打开 `chrome://extensions/`；Edge 打开 `edge://extensions/`。
4. 开启“开发者模式”，点击“加载已解压的扩展程序”，选择本目录；如果使用 ZIP，请先解压。
5. 点击扩展图标，填写 VideoRoll 地址和 Token，然后点击“保存并授权访问”。

例如 Web 地址为 `http://192.168.1.9:3001` 时，可以直接填写该地址；扩展会自动使用：

```text
http://192.168.1.9:3001/api/remote/auto/youtube
```

## 使用

- 在视频缩略图链接上右键，选择“提交到 VideoRoll 自动模式”。
- 在视频播放页空白处右键，也会提交当前视频。
- 播放器可能先显示 YouTube 自己的菜单，再右键一次即可显示浏览器原生菜单。
- 页面右上角会显示提交结果；扩展图标徽章也会显示处理中、成功或失败状态。

## 安全与重试

- Token 仅保存在浏览器扩展的本地存储中，不会放入 URL、网页 DOM 或日志。
- 扩展只会向你保存并授权的 VideoRoll 地址发送 Token。
- 网络超时、连接中断或服务端 `5xx` 时，会保留该视频的幂等键。再次右键提交同一视频会复用该键，避免第一次请求实际成功后又重复派发。
- 幂等键最多保留 23 小时，与 VideoRoll Remote API 的 24 小时幂等窗口配合。
