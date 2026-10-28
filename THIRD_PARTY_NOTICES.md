# Third-Party Notices / 第三方组件与模型声明

本项目（vtw）的**源代码**以 MIT 发布，见 [LICENSE](LICENSE)。

本文件列出运行期依赖与模型权重的许可证信息，以及分发二进制时需要注意的义务。
**本仓库只分发源代码**：第三方依赖由 `pip install -r requirements.txt` 从 PyPI 安装，
模型权重由 `scripts/download_models.py` 从上游 release 下载；仓库本身不包含这些二进制或权重。

> 本文件不是法律意见。商业发行或打包二进制前，请自行完成许可证合规审查。

---

## 1. 运行期依赖

| 组件 | 版本 | 许可证 | 用途 / 备注 |
|---|---|---|---|
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | 1.13.8 | Apache-2.0 | 识别/VAD 推理；`sherpa-onnx-core` 同为 Apache-2.0 |
| [PyAV](https://github.com/PyAV-Org/PyAV) | 19.0.2 | **源码 BSD-3-Clause；PyPI wheel 另含 GPL/LGPL 组件** | 音频文件解码，详见第 2 节 |
| [sounddevice](https://github.com/spatialaudio/python-sounddevice) | 0.5.6 | MIT | 麦克风采集 |
| [SoundCard](https://github.com/bastibe/SoundCard) | 0.4.6 | BSD-3-Clause | WASAPI 环回 |
| [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) | 0.2.12.8 | Apache-2.0 | 环回备用后端 |
| [soxr](https://github.com/dofuuz/python-soxr) | 1.1.0 | LGPL-2.1-or-later | 重采样（可选，缺失时退回线性插值） |
| [keyboard](https://github.com/boppreh/keyboard) | 0.13.5 | MIT | 热键兜底 |
| [pystray](https://github.com/moses-palmer/pystray) | 0.19.5 | LGPL-3.0 | 系统托盘 |
| [Pillow](https://python-pillow.org/) | 12.3.0 | MIT-CMU (HPND) | 托盘图标绘制 |
| [numpy](https://numpy.org/) | 2.5.3 | BSD-3-Clause（另含 0BSD/MIT/Zlib/CC0 等） | 数值计算 |
| [cffi](https://cffi.readthedocs.io/) | 2.1.1 | MIT-0 | `sounddevice` / `SoundCard` 的传递依赖 |
| [pycparser](https://github.com/eliben/pycparser) | 3.0 | BSD-3-Clause | `cffi` 的传递依赖 |
| [six](https://github.com/benjaminp/six) | 1.17.0 | MIT | `pystray` 的传递依赖 |

版权归各项目作者所有；完整许可文本请见各组件源码仓库或安装包内的 `LICENSE` / `dist-info/licenses`。
`sherpa-onnx` 的 wheel 可能同时包含 ONNX Runtime 等运行时组件（通常为 MIT），
若分发该 wheel，请一并保留其 `dist-info` 中的声明。

---

## 2. 重点：PyAV wheel 的 GPL/LGPL 情况

- **PyAV 源码**采用 BSD-3-Clause（见其 `LICENSE.txt`）。
- **PyPI 上的 Windows binary wheel** 会捆绑 FFmpeg 动态库，且实际包含
  `libx264`、`libx265` 等 **GPL** 组件（例如 `av-19.0.2` 的 `av.libs/` 中可看到
  `libx264-*.dll`、`libx265-*.dll`）。
- PyAV 维护者在 issue #2270 中对 binary wheel 的说明是：
  **“LGPLv2.1 or later + Commercial exceptions” 或 “GPLv2 or later”**。
  PyPI 元数据里的 `License-Expression: BSD-3-Clause` 只描述 PyAV 源码，不足以覆盖 wheel 内的 FFmpeg。

**对本项目的含义**：

- 本项目只提供源码，不打包 PyAV wheel；用户通过 pip 自行安装时，由用户与 PyAV/FFmpeg 的许可关系决定。
- 如果你把 PyAV、FFmpeg 与应用一起打包成 EXE/安装器/Docker 镜像，则属于再分发：
  - 需按 **GPLv2+** 提供相应源码与许可，或
  - 使用 **LGPL-only** 的 FFmpeg 构建并满足 LGPL 条件，或
  - 取得 x264/x265 的商业例外（不能默认其存在）。
- 不要上传包含 `.venv` 的压缩包；那会直接再分发 GPL/LGPL 二进制。

参考：<https://github.com/PyAV-Org/PyAV/issues/2270>

---

## 3. 模型权重

模型权重**不在本仓库中**，由 `scripts/download_models.py` 从 sherpa-onnx 官方
release 下载：

| 模型 | 上游 | 许可 |
|---|---|---|
| SenseVoice int8（中英日韩粤） | [FunAudioLLM/SenseVoice](https://github.com/FunAudioLLM/SenseVoice) / [FunASR](https://github.com/modelscope/FunASR) | **代码 MIT；权重 FunASR Model License 1.1** |
| silero VAD v5 | [snakers4/silero-vad](https://github.com/snakers4/silero-vad) | MIT |

### 3.1 SenseVoice

- **代码**：MIT（Copyright 2025 FunASR）；
- **SenseVoiceSmall / 下载的 int8 权重**：Hugging Face 模型卡标注
  `license: other` / `model-license`，指向
  [FunASR Model License 1.1](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)；
- 本仓库下载的 `sherpa-onnx-sense-voice-...` 压缩包内的 `LICENSE` 也只是引用该 FunASR 许可。

**FunASR Model License 1.1 的主要义务**（以原文为准）：

- 允许使用、复制、修改、分享模型权重及其衍生品；
- 必须**注明出处与作者信息**，并**保留相关模型名称**；
- 模型“仅作为参考和学习使用”，商业使用前应自行评估/咨询律师；
- 不得对模型进行无端诋毁、恶意抹黑或凭空谩骂，否则许可自动终止；
- 许可可能更新并自动生效。

**建议署名**（可放在 README / 关于对话框 / 发布页）：

> 本项目使用 SenseVoice（FunAudioLLM / FunASR，阿里巴巴）作为语音识别模型，
> 使用 Silero VAD（Silero Team）作为语音活动检测模型。模型权重遵循其各自上游许可。

### 3.2 Silero VAD

- 上游仓库采用 MIT（Copyright 2020-present Silero Team）；
- 若再分发 `silero_vad_v5.onnx`，请保留 MIT 版权与许可声明；
- 本项目不在仓库中分发该模型文件。

---

## 4. 分发二进制时的义务清单

如果你计划发布 **EXE / 安装器 / Docker 镜像 / 含依赖的压缩包**，至少需要：

1. **GPL 组件（PyAV wheel 内的 FFmpeg/x264/x265）**：按 GPLv2+ 提供对应源码与许可，
   或改用 LGPL-only 构建/取得商业例外；
2. **LGPL 组件（pystray、soxr、FFmpeg LGPL 部分）**：附 LGPL 许可证、源码获取方式，
   并满足可替换/重链接要求；
3. **Apache-2.0 组件（sherpa-onnx、PyAudioWPatch）**：保留 LICENSE/NOTICE；
4. **BSD/MIT 组件**：保留版权声明与免责声明；
5. **模型权重**（若打包）：遵守 FunASR Model License 的署名/保留模型名等要求；
6. **专利**：AAC/MP3 等编解码器可能涉及独立专利许可，与著作权/开源许可证无关。

**推荐做法**：保持本仓库为纯源码分发，由用户自行 `pip install` 与下载模型；
这是当前风险最低的方式。

---

## 5. 完整许可证文本

源码仓库只需保留本声明与链接；若分发二进制，请把对应许可证全文一并附上：

- MIT：<https://opensource.org/license/mit>
- Apache-2.0：<https://www.apache.org/licenses/LICENSE-2.0>
- BSD-3-Clause：<https://opensource.org/license/bsd-3-clause>
- LGPL-2.1：<https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html>
- LGPL-3.0：<https://www.gnu.org/licenses/lgpl-3.0.html>
- GPL-2.0：<https://www.gnu.org/licenses/old-licenses/gpl-2.0.html>
- FunASR Model License 1.1：<https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE>

---

*本文件只做许可证信息汇总，不构成法律意见。*
