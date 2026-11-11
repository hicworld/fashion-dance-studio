# 配置与工作流

## 云端配置

在工作台的「API设置」填写密钥，或在本机 `.env` 配置。按所选功能设置：

| 用途 | 所需配置 |
| --- | --- |
| Seedream 图片合成、Seedance 2.5 | `ARK_API_KEY`，并在方舟开通对应模型 |
| 即梦动作模仿 | 火山 AK/SK 和 TOS 存储桶、Endpoint、地域 |
| Seedance 角色替换 | 方舟密钥；普通文件复用 TOS。符合平台要求的可信素材可填写素材 ID |
| MiniMax H3 / Speech | `MINIMAX_API_KEY`，分别确认视频或语音的服务权限和按量余额 |
| fal 可灵 | `FAL_KEY` |
| ElevenLabs 音效 | `ELEVENLABS_API_KEY` |

TOS 的 AK/SK 可以单独配置，留空时会尝试复用火山 AK/SK。请查看 [配置示例](../.env.example) 中的变量名。界面显示已配置表示本机参数齐全，账号授权和余额由服务端验证。

## 穿搭和角色替换

1. 新建创作，选择穿搭动作或角色替换，再选择对应的视频模型。
2. 上传输入素材，选择开始位置和时长。建议先试约 5 秒；各模型支持的时长和分辨率在界面中显示。
3. 角色替换需要为每张角色图描述它对应的原片人物。预览显示输入首帧、角色映射等信息，供核对。
4. 确认后提交视频生成。若使用穿搭图片合成，图片阶段会另行调用 Seedream。
5. 已取得云端任务编号的请求可按原编号续查。提交状态未知时，先核对服务商任务记录，再决定是否另开任务。

方舟对写实人像有可信素材要求，外部 AI 人物图片也可能触发人像审核。请按 [方舟肖像素材规则](https://docs.volcengine.com/docs/ark/seedance-portrait-asset-guide?lang=zh) 选用允许的素材，并在「Seedance 授权素材」中按顺序填写对应的素材 ID。MiniMax 的 HTTP 402 表示应检查开放平台按量余额。

## 短片制作

新建一个 ZIP，放入分镜 JSON 和所引用的首帧图。以下是一个可自行配图的最小示例：

```json
{
  "metadata": {"title": "纸船试镜"},
  "shots": [
    {
      "id": "S01",
      "title": "纸船浮在水面",
      "duration_seconds": 5,
      "video_prompt": "固定镜头，一艘折纸小船在浅水中缓慢漂过。",
      "generated_start_frame_file": "paper-boat.png",
      "sound": "轻微水声。"
    }
  ]
}
```

把自有的 `paper-boat.png` 和 JSON 一起压缩，再导入「短片制作」。先检查分镜和首帧，再选择要生成的镜头、同步声音和后处理。需要先试本地流程时，可从「素材与剪辑」编辑已完成片段，并在「全片声音制作」导入本地音频。

云端生成和本地导出分别保存，已完成的原片可继续裁切或超分。导出时可查看分镜试听片段，或者合成时间线预览。

## 调试

- **FFmpeg 不可用**：在新终端运行 `ffmpeg -version` 和 `ffprobe -version`，确认二者在 PATH 中。
- **镜头 AI 超分不可用**：运行 `python prepare_realesrgan.py --check` 检查本地文件；无组件时选择普通缩放。
- **云端查询暂未完成**：记录中有任务 ID 时使用续查，不需要再次提交生成。
- **桌面窗口打不开**：确认 WebView2 Runtime 已安装。开发时也可运行 `python app.py` 用浏览器打开工作台。

Liblib 保留了旧任务适配代码供维护，当前工作台不提供该模型选项；[工作流配置示例](../liblib-workflow.example.json) 不含可调用的真实模板 ID。
