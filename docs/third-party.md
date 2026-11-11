# 第三方组件

这个仓库交付源码。FFmpeg、桌面运行库和超分模型通过各自的来源安装，未随 Git 提交。

- **FFmpeg / FFprobe**：从 [FFmpeg 下载页](https://ffmpeg.org/download.html) 获取 Windows 构建，并将 `bin` 加入 PATH。FFmpeg 的许可取决于所用构建和选项，见 [FFmpeg 法律与许可说明](https://ffmpeg.org/legal.html)。
- **Real-ESRGAN ncnn Vulkan**：来源和 SHA-256 在 [SOURCE.json](../third_party/realesrgan/SOURCE.json)。运行 `python prepare_realesrgan.py` 可从指定上游发布恢复组件；已有官方 ZIP 时可使用 `--archive 路径`；`--check` 仅校验本机文件。下载不运行引擎。随仓库保留 [Real-ESRGAN](../third_party/realesrgan/LICENSE-Real-ESRGAN.txt)、[ncnn Vulkan](../third_party/realesrgan/LICENSE-ncnn-vulkan.txt) 与 [ncnn](../third_party/realesrgan/LICENSE-ncnn.txt) 的许可说明。
- **Python 依赖**：在 [requirements.txt](../requirements.txt) 和 [requirements-desktop.txt](../requirements-desktop.txt) 中列出，分别使用各自许可证。桌面界面使用 pywebview 和系统 Edge WebView2 Runtime。
- **默认模特和图标**：默认模特图为 AI 生成的示例人物；应用图标由本项目生成。

如果要对外分发编译版，需按实际打包版本核对 FFmpeg、Visual C++ 运行库及其他依赖的再分发要求。
