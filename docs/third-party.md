# 第三方组件

源码仓库不提交 FFmpeg、桌面运行库或超分模型。Windows 便携 ZIP 包含应用及其 Python 运行依赖；FFmpeg 和超分组件由用户电脑从上游取得。

- **FFmpeg / FFprobe**：便携版可复用 PATH 上的安装，或在首次启动时下载 [ffmpeg-win64.json](../ffmpeg-win64.json) 锁定的 Gyan Windows 构建，校验 ZIP 与两个 EXE 后安装到本机数据目录。也可从 [FFmpeg 下载页](https://ffmpeg.org/download.html) 自行安装。许可证见 [FFmpeg 许可说明](https://ffmpeg.org/legal.html)。
- **Real-ESRGAN ncnn Vulkan**：来源和 SHA-256 在 [SOURCE.json](../third_party/realesrgan/SOURCE.json)。运行 `python prepare_realesrgan.py` 可从指定上游发布恢复组件；已有官方 ZIP 时可使用 `--archive 路径`；`--check` 仅校验本机文件。下载不运行引擎。随仓库保留 [Real-ESRGAN](../third_party/realesrgan/LICENSE-Real-ESRGAN.txt)、[ncnn Vulkan](../third_party/realesrgan/LICENSE-ncnn-vulkan.txt) 与 [ncnn](../third_party/realesrgan/LICENSE-ncnn.txt) 的许可说明。
- **Python 依赖**：在 [requirements.txt](../requirements.txt) 和 [requirements-desktop.txt](../requirements-desktop.txt) 中列出，分别使用各自许可证。分发包内的 `licenses/` 收录实际打包组件的通知。桌面版依赖 Windows 10/11 x64、系统 Edge WebView2；若缺少 VC++ 运行库，可从 [微软官方页面](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist) 安装。
- **默认模特和图标**：默认模特图为 AI 生成的示例人物；应用图标由本项目生成。

## 便携版构建

在 Windows x64 环境创建 `.venv` 后运行：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-desktop.txt
.\.venv\Scripts\python.exe collect_release_notices.py --prepare-upstream
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_desktop.ps1 -SkipInstall -PortableRelease -OutputDirectory dist_portable
.\.venv\Scripts\python.exe package_portable_release.py "dist_portable/穿搭舞蹈工作室" "dist_portable/FashionDanceStudio-windows-x64.zip"
```

便携版不携带 FFmpeg、Real-ESRGAN 模型或独立的微软 VC/UCRT 运行库。构建后会运行无云端请求的本机启动检查。普通开发构建仍可捆绑本机媒体组件供自己使用。
