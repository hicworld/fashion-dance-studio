# 穿搭舞蹈工作室 · Fashion Dance Studio

一个在 Windows 上运行的 AI 视频工作台。它把素材整理、云端模型调用、任务恢复和本地剪辑放在同一个界面里。可直接用浏览器运行源码，也可打包成桌面应用。

## 功能

- **穿搭动作**：合成穿搭图或导入成图，选择舞蹈片段、背景和动作模型。
- **视频角色替换**：上传原视频与最多三张角色图，逐个核对人物对应关系后提交。
- **分镜短片**：导入分镜 JSON 和首帧图，逐镜生成；支持断点续查、裁切、试剪和本地超分。
- **素材与声音**：编辑时间线，混合对白、音效、环境声和音乐；也可以直接导入本地音频。

生成前会显示素材配置、预览或预计用量。提交状态不明时，程序保留原记录，避免自动再发收费请求。具体模型的素材限制、费用和用量以服务商为准。

## Windows 快速启动

安装 **Python 3.11** 和 [FFmpeg](https://ffmpeg.org/download.html)，确保终端能运行 `ffmpeg -version` 和 `ffprobe -version`。在项目目录打开 PowerShell：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
```

打开 [本地工作台](http://127.0.0.1:7860)。在「API设置」填写所用平台的凭据；也可以复制 `.env.example` 为 `.env` 后填写。只需配置你要使用的服务。Windows 用户也可以双击 `启动.cmd`。

**本地预览、裁切、剪辑和导入音频不需要云端密钥。** 图片合成、视频生成和云端声音合成需要对应服务的账号权限；开启这些功能会将所选素材提交给该服务。

| 工作流 | 支持的服务 |
| --- | --- |
| 穿搭图片与背景 | 火山方舟 Seedream |
| 动作迁移 | 即梦动作模仿 2.0、fal 可灵 |
| 角色替换 | MiniMax H3、方舟 Seedance 2.5、fal Kling O3 |
| 分镜短片 | 方舟 Seedance 2.5 |
| 全片声音 | MiniMax Speech、ElevenLabs 或本地导入 |

见 [配置与工作流](docs/workflows.md)，包括密钥位置、分镜 ZIP 格式和常见错误。

## 本地打包桌面版

需要 Windows、Edge WebView2 Runtime，以及上面的 Python 和 FFmpeg。Real-ESRGAN 二进制和模型由上游发布提供；脚本会按锁定版本下载并验证 SHA-256：

```powershell
.\.venv\Scripts\python.exe prepare_realesrgan.py
powershell -NoProfile -ExecutionPolicy Bypass -File .\build_desktop.ps1
```

生成的程序位于 `dist/穿搭舞蹈工作室/`。源码模式下超分组件可选，没下载时可选择普通缩放。组件来源、许可和本地校验方式见 [第三方组件说明](docs/third-party.md)。

## 本地测试

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover
```

测试使用合成素材和模拟 API；媒体处理测试会调用本机 FFmpeg。仓库的 GitHub Actions 使用 Windows 运行同一套测试，无需设置服务商密钥。

## 本机数据

源码运行时，`.env`、`jobs/` 和 `shortfilms/` 默认保存在源码目录；桌面版默认保存在用户目录下的 `FashionDanceStudio`。可通过 `FASHION_DANCE_HOME` 指定独立的数据目录。请备份数据目录以保留任务和配置。Git 忽略规则已排除这些数据、研究记录和构建产物。

## 许可

本项目源码尚未指定开源许可证。第三方组件使用各自的许可，见 [第三方组件说明](docs/third-party.md)。
