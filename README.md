# vtw —— 完全本地的语音转文字工具

> A fully offline, Windows-only speech-to-text tray app powered by SenseVoice.
> **No audio ever leaves your machine.**

Windows 常驻托盘、开机自启、按住热键说话就把文字打到光标处的离线语音输入工具。
**不上传任何音频，运行期零网络请求。**

支持 Windows 10/11 x64 · Python 3.14 · SenseVoice 中英日韩粤识别。

> **许可范围**：本仓库**源代码**采用 MIT；模型权重（SenseVoice）与第三方依赖
> （如 PyAV/FFmpeg）分别适用其自身许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

---

## 特性

| 能力 | 说明 |
|---|---|
| 完全离线 | 模型与依赖在安装阶段一次性下载；之后断网也能用 |
| 麦克风听写 | 按住 `Ctrl+Alt+Space` 说话，松开即识别并把文字输入到当前光标处 |
| 会议转写 | 按 `Ctrl+Alt+A` 实时字幕 + 自动落盘；音源可选**系统声音**（在线会议）、**麦克风**（线下会议）或两者 |
| 音频文件转写 | 托盘选择 `WAV / MP3 / M4A / FLAC / OGG` 等文件，实时进度 + 逐句落盘，可随时取消 |
| 开机自启 | 登录即自动运行，**无控制台黑框**（用 `pythonw.exe` 启动） |
| 低占用 | 空闲 CPU **0%**、内存约 **358MB**；识别速率约为实时的 **40 倍** |
| 高准确率 | 中文样例与官方 ground truth **逐字一致**；英文正确保留大小写与数字归一化 |
| 中英日韩粤 | SenseVoice 原生支持，自动识别语种 |
| 自动标点 | 模型根据语气停顿自动补 `，` `。` `？` |
| 口述标点 | 说「逗号」「句号」「换行」等直接插入对应符号（可关） |
| 图形设置 | 托盘右键 →「设置…」改快捷键、听写模式、会议音源、口述标点、开机自启 |
| 轻量 UI | UI 用标准库 tkinter；全局热键用自研 Win32 钩子，无第三方热键库 |

---

## 系统要求

- **Windows 10/11 x64**（依赖 WASAPI、Win32 键盘钩子、注册表自启，不能跨平台）
- **Python 3.14 x64**（安装时勾选 `Add Python to PATH`；tkinter 随官方安装包提供）
- 约 **600 MB** 磁盘：依赖约 200 MB，模型约 390 MB
- 仅安装阶段需要联网；运行期完全离线

---

## 快速开始

### 一键部署（推荐）

```powershell
# 安装依赖 + 下载模型，不注册开机自启
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -SkipAutostart

# 日常使用：注册开机自启
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1
```

脚本幂等，可重复运行；已经完成的步骤会自动跳过。

部署完成后可以直接双击 **`run.pyw`** 启动（无控制台窗口）。

> 模型权重遵循上游 **FunASR Model License 1.1**；本项目代码为 MIT，两者的许可范围不同，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

### 手动安装

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe scripts\download_models.py

# 启动（run.pyw 会自动把 src 加入 sys.path）
.\.venv\Scripts\python.exe run.pyw
```

想看到实时日志：

```powershell
.\.venv\Scripts\python.exe -m vtw -v     # 需先执行过 setup.ps1（会写入 vtw.pth）
# 或
$env:PYTHONPATH = "$PWD\src"
.\.venv\Scripts\python.exe -m vtw -v
```

---

## 使用方法

### 听写（麦克风 → 光标处）

1. 把光标放到目标输入框（记事本 / 微信 / 浏览器 / Word 均可）
2. **按住** `Ctrl+Alt+Space`，屏幕底部出现红色浮条表示正在录音
3. 说完**松开**，文字会自动输入到光标处

> 热键被抑制，按住时**不会**往目标程序里插入空格。
> 当前是「按住说话」模式；也可以在设置里改成「按一下开始/结束」。

### 会议 / 系统声音转写

- 按 `Ctrl+Alt+A` 开始，再按一次停止
- 会弹出字幕窗口实时显示，并同步写入 `output\会议-<时间戳>.txt`（带时间戳）
- 停止后托盘会提示文字稿路径

**音源可选**（托盘右键 →「会议音源」，或设置窗口里的「会议音源」）：

| 音源 | 适用场景 | 说明 |
|---|---|---|
| 系统声音（默认） | 在线会议、网课、视频 | 抓取电脑正在播放的声音 |
| 麦克风 | **线下会议**、当面讨论 | 抓取房间里说话的声音，不需要系统播放任何东西 |
| 系统声音 + 麦克风 | 戴耳机开在线会 | 同时录到对方和你自己；外放时同一句话会被录两遍，建议戴耳机 |

音源切换在**下次开始会议转写时**生效。

### 音频文件转写

- 托盘右键 →「**转写音频文件…**」→ 选择本地音频文件
- 会弹出进度窗口：实时显示百分比的已转写句子；点「取消」可随时停止，**已转写部分会保留**
- 完成后文字稿保存到 `output\转写-<文件名>-<时间戳>.txt`，路径显示在窗口底部
- 窗口里的文字可以直接选中复制
- 支持格式：**WAV** 原生支持；**MP3 / M4A / AAC / FLAC / OGG / OPUS / WMA** 以及带音轨的
  **MP4 / MKV** 由 PyAV（wheel 自带 FFmpeg）解码，安装依赖时已一并装好

长文件按 0.5 秒一块流式解码，不会把整段音频读进内存；识别每完成一句就立即写盘，
中途取消或程序崩溃都不会丢掉已转写的内容。

### 托盘菜单

右键托盘图标可以：开始/结束听写、开始/停止会议转写、显示字幕窗口、切换会议音源、
**转写音频文件…**、显示文件转写窗口、**设置…**、切换听写模式（按住说话 / 按一下开始）、
重新载入配置、查看历史、打开输出目录、打开配置文件、打开日志、关于、退出。

托盘图标颜色即状态：**灰**＝空闲、**红**＝录音、**黄**＝识别中、**绿**＝会议转写。

### 口述标点

模型自带**自动标点**（根据停顿补 `，` `。` `？`），但**不会**把说出口的标点词转成符号。
所以本工具加了一层后处理：说「逗号」「句号」等，会直接插入对应符号。

支持的词：

| 类别 | 说这些 | 得到 |
|---|---|---|
| 中文 | 逗号 句号 问号 感叹号 / 叹号 分号 冒号 顿号 | `，` `。` `？` `！` `；` `：` `、` |
| 中文 | 省略号 破折号 左/右引号 左/右括号 书名号 | `……` `——` `“”` `（）` `《》` |
| 中文 | 换行 回车 换段 空格 | 换行 / 空行 / 空格 |
| 英文 | comma period / full stop question mark exclamation mark | `,` `.` `?` `!` |
| 英文 | colon semicolon new line / newline new paragraph | `:` `;` 换行 / 空行 |

实际效果（用 Windows 自带 TTS 合成的语音实测）：

```
说：大家好逗号今天天气不错句号请问你叫什么名字问号感叹号换行新的一行

模型原始输出：大家好，逗号，今天天气不错，句号，请问你叫什么名字？问号感叹号换行新的异行。
开启口述标点：大家好，今天天气不错。请问你叫什么名字？！
              新的异行。
```

可以看到模型给标点词前后补的自动标点会被一并吃掉，不会出现 `大家好，，，今天`。

> ⚠️ **已知限制**：中文没有词间空格，无法区分"说标点"和"谈标点"。
> 说「这个**逗号**用法不对」会被转成「这个，用法不对」。
> 遇到这种情况，去设置界面把「识别口述的标点」关掉即可（英文同样存在
> `period` 兼指"时期"的问题）。

---

## 设置界面

右键托盘 →「**设置…**」，可以在界面里直接改：

- **快捷键**：点一下输入框，然后**直接按下你想用的组合键**即可（按 `Esc` 取消）。
  支持 `ctrl+alt+space` 这种组合，也支持单独一个修饰键（例如只按**右 Ctrl**）。
  听写热键与会议热键不允许相同。
- **听写模式**：按住说话 / 按一下开始结束。
- **会议音源**：系统声音 / 麦克风 / 系统声音 + 麦克风（见上一节）。
- **口述标点**：是否识别说出口的标点词（详见上节）。
- **开机自启**：勾选框直接开关，并显示当前注册到注册表的启动命令。

点「保存」后**热键立即生效，不需要重启程序**。

> 实现细节：录制快捷键时程序会**临时暂停全局热键钩子**。否则用户按下的
> `Ctrl+Alt+Space` 会被自己的钩子吞掉，根本传不到设置窗口 —— 表现为
> "点进去按什么都没反应"。

---

## 配置

首次运行会自动生成 `config.json`；也可以把 `config.example.json` 复制为
`config.json` 后修改。编辑后从托盘菜单选「重新载入配置」即可生效
（热键与线程数改动需要重启）。文件损坏时会自动备份为 `config.json.bad` 并用默认值重建。

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `hotkey_dictate` | `ctrl+alt+space` | 听写热键。按住说话模式下是"按住"键 |
| `hotkey_meeting_toggle` | `ctrl+alt+a` | 会议转写开关 |
| `mode` | `push_to_talk` | `push_to_talk` 按住说话 / `toggle` 按一下开始结束 |
| `meeting_source` | `loopback` | 会议音源：`loopback` 系统声音 / `mic` 麦克风 / `both` 两者 |
| `input_device` | `default` | 麦克风设备名或索引（`--list-devices` 可查） |
| `loopback_device` | `default` | 系统声音环回设备 |
| `num_threads` | `4` | 识别线程数。CPU 核多可调到 6~8 更快 |
| `use_itn` | `true` | 数字/标点归一化（"九点"→"9点"、"五十"→"50"）|
| `english_sentence_case` | `true` | 整句大写的英文还原为正常大小写 |
| `spoken_punctuation` | `true` | 口述标点词转成符号（"逗号"→"，"），见上节 |
| `lowercase_output` | `false` | 是否强制全小写。**默认关闭以保留英文大小写** |
| `inject_method` | `clipboard` | `clipboard` 剪贴板粘贴（推荐）/ `unicode` 逐字注入 |
| `restore_clipboard` | `true` | 注入后恢复原剪贴板**文本**内容 |
| `preload_model` | `true` | 启动后台预热模型，首次呼出无需等待 |
| `warmup_delay_seconds` | `8` | 推迟预热，避免拖慢登录 |
| `idle_unload_seconds` | `0` | >0 时空闲超时卸载模型回收内存（内存可降到约 120MB）|
| `max_dictation_seconds` | `120` | 单次录音上限，超过自动结束 |
| `overlay_position` | `bottom-center` | 录音浮条位置；设 `off` 或 `overlay_enabled:false` 可关闭 |
| `save_meeting_audio` | `false` | 会议转写时是否另存 WAV |

---

## 自检脚本

部署或排错时按顺序跑这几个，每个都会打印明确的中文结论：

```powershell
.\.venv\Scripts\python.exe scripts\smoke_asr.py       # 模型加载 + 识别准确率 + RTF
.\.venv\Scripts\python.exe scripts\test_hotkey.py     # 热键触发与按键抑制
.\.venv\Scripts\python.exe scripts\test_injection.py  # 识别→剪贴板/SendInput→窗口
.\.venv\Scripts\python.exe scripts\test_meeting.py    # 扬声器播放→环回采集→识别
.\.venv\Scripts\python.exe scripts\test_meeting_gui.py  # 会议窗口→字幕→落盘（加 --source mic 测麦克风）
.\.venv\Scripts\python.exe scripts\test_file_transcribe.py  # 音频文件→解码→VAD→识别→落盘（可 --file 指定文件）
.\.venv\Scripts\python.exe scripts\test_mic.py        # 麦克风采集→识别
.\.venv\Scripts\python.exe scripts\test_settings_gui.py  # 设置界面：录制→保存→新热键即时生效
.\.venv\Scripts\python.exe scripts\test_punctuation.py    # 合成语音→识别→口述标点转换
.\.venv\Scripts\python.exe scripts\dictate_once.py --inject   # 手动录一段试试
.\.venv\Scripts\python.exe -m unittest discover -s tests      # 单元测试
```

> 这些脚本会访问真实麦克风、扬声器、全局热键与桌面窗口，需在交互式 Windows 桌面上运行。
> `test_settings_gui.py` 会临时改写本机热键与 `config.json`（结束会还原）。

---

## 排错

**热键没反应**
：可能被别的软件占用了。改 `config.json` 里的 `hotkey_dictate`，推荐
`ctrl+alt+d`、`ctrl+shift+space`、`alt+\``。改完重启程序。

**用 `alt+某键` 时，某些程序会弹出菜单栏**
：因为 Alt 键本身被放行（只吞主键），个别程序会把"单独按下再松开 Alt"
当成激活菜单。如果遇到，换成 `ctrl+alt+*` 这类组合更稳妥（设置界面里改即可）。

**按住热键时目标程序里混进空格**
：跑 `scripts\test_hotkey.py` 确认抑制状态。正常应输出「Space 被完全吞掉」。

**没有输出文字**
：先确认光标在可输入的输入框里；再看 `logs\vtw.log`。
部分程序（如某些终端、游戏）不吃 `Ctrl+V`，可把 `inject_method` 改成 `unicode` 试试。

**麦克风没声音**
：检查 Windows 设置 → 隐私和安全性 → 麦克风，是否允许桌面应用访问；
并用 `scripts\dictate_once.py --list-devices` 确认设备。

**麦克风声音小 / 说一句只识别出一两个字**
：程序已做低音量补偿：静音裁剪改为自适应阈值，识别前会把低电平音频峰值归一化，
日志里会打印 `听写音频：原始 3.20s（峰值 0.0321），裁剪后 3.10s`。
如果峰值低于 0.001，程序会直接提示「没有听到声音」而不是硬识别。
仍不正常时，请在 Windows 声音设置里确认输入设备选对、麦克风未被静音、增益不要太低
（控制面板 → 声音 → 录制 → 属性 → 级别）。

**会议转写抓不到声音**
：先看会议音源是否选对 —— 在线会议选「系统声音」，线下会议选「麦克风」
（托盘右键 →「会议音源」）。系统声音模式要确认电脑确实在播放且没有静音；
若 `soundcard` 后端在本机声卡上不可用，程序会自动回退到 `pyaudiowpatch` + `soxr` 重采样。

**线下会议只有房间里说话，没有系统播放**
：托盘右键 →「会议音源」→「麦克风（线下会议）」，再按 `Ctrl+Alt+A` 开始。
麦克风模式直接用系统默认输入设备（可在 `input_device` 里改）。

**模型缺失/损坏**
：`python scripts\download_models.py --check` 查看状态，`--force` 重新安装
（优先复用 `models\_downloads\` 里的本地缓存，无需重新联网）。

**想彻底卸载**
：`powershell -File scripts\uninstall_autostart.ps1` 取消自启，
然后直接删除整个项目目录即可（不写系统目录、不改全局 pip 配置）。

---

## 已知限制

- **仅支持 Windows**：系统声音环回、全局键盘钩子、开机自启都依赖 Win32/WASAPI。
- **剪贴板恢复只覆盖文本**。若注入前剪贴板里是图片/文件，恢复后那部分内容会丢失
  （日志会给出提示）。识别结果本身不会丢，始终会写入历史记录。
- **非文本格式剪贴板的提示**：这是有意的，避免静默丢数据。
- **麦克风与会议同开**时，对着麦克风说话可能被系统声音环回重复采集一次（会提示一次）。
- 音频文件转写**一次一个**，暂不支持批量队列/文件夹递归；视频文件只取其中的音频轨道。
- 不做 GPU/NPU 加速：CPU 上已经比实时快约 40 倍，引入 GPU 依赖得不偿失。

---

## 技术选型说明

这些结论都是在本机实测后定下来的，改动前请先看原因：

**为什么用 SenseVoice `2024-07-17` int8 而不是更新的 `2025-09-09`**
在官方样例上对比了三个候选：

| 模型 | 中文（ITN 开） | 英文（ITN 开） | RTF |
|---|---|---|---|
| **2024-07-17 int8（选用）** | `开饭时间早上9点至下午5点。` ✅ 等于真值 | `The tribal chieftain ... with 50 pieces of code.` ✅ 等于真值 | 0.106 |
| 2025-09-09 int8 | `放时间早上九点至下午五点` ❌ 掉字 | `THE TRIVBAL CHIEF THIN ... COOD` ❌ 全大写且错词 | 0.032 |
| funasr-nano 2025-12-17 | `开饭时间早上九点至下午五点` 无归一化 | `the tribal chiefin ...` ❌ 无 ITN | 0.024 |

2025-09-09 虽然快 3 倍，但中文掉字、英文整句大写且识别错误更多，不满足"准确率高"。

**为什么不用 `keyboard` 库做全局热键**
`keyboard` 最后发版是 2020 年，实测 `add_hotkey(..., suppress=True)`
第一次触发失败、第二次才成功，抑制行为依赖库内部脆弱的按键状态机。
现改为自研 `WH_KEYBOARD_LL` 钩子（`src/vtw/nativehook.py`），行为完全确定：
只吞组合键的主键、修饰键照常放行，长按产生的重复 keydown 只触发一次回调。

**为什么 `sounddevice` 不能做系统声音采集**
已读源码确认其 `WasapiSettings` **没有** `loopback` 参数。
因此环回采集用 `soundcard`，失败时回退 `pyaudiowpatch` + `soxr`。

**为什么文件转写用 PyAV 而不是外部 ffmpeg**
PyAV 的 wheel 自带 FFmpeg 动态库，`pip install` 后即可解码 MP3/M4A/FLAC/OGG，
用户不需要另外安装命令行工具，也不依赖 PATH。WAV 走标准库，即使没有 PyAV 也能用；
缺少 PyAV 时其它格式会给出明确的中文安装提示。

**为什么 `SendInput` 要带扫描码**
`inject.py` 同时设置 `wVk` 与 `wScan`。仅设 `wVk` 而 `wScan=0` 时，
依赖扫描码做键盘布局翻译的程序可能解析不出按键。原生控件上两种写法都通过，
带扫描码是 Windows 文档推荐做法，兼容性更好。

**为什么 `.ps1` 带 UTF-8 BOM**
Windows PowerShell 5.1 会把无 BOM 的 `.ps1` 按 ANSI(GBK) 解码，
中文注释会导致语法错误。**编辑这些脚本后请保留 BOM。**

---

## 性能实测（开发机）

| 指标 | 实测值 | 目标 |
|---|---|---|
| 模型加载 | 1.29 ~ 1.90 s | — |
| 中文识别 RTF（5.59s 音频） | **0.0246**（约 41× 实时） | < 0.2 |
| 英文识别 RTF | 0.0335 | < 0.2 |
| 空闲 CPU | **0 %** | < 0.5% |
| 空闲内存（工作集） | **355 ~ 358 MB** | < 500 MB |
| 单元测试 | **218 项全通过**（缺模型/无声卡时相关用例自动跳过） | — |
| 会议界面端到端（`test_meeting_gui.py`） | 系统声音 / 麦克风 / 两者均通过：窗口创建、实时字幕、文字稿落盘 | — |
| 文件转写端到端（`test_file_transcribe.py`） | WAV / 44.1kHz 立体声 / MP3 / M4A 均通过，RTF ≈ **0.030** | < 0.2 |
| 设置界面尺寸 | 613 × 734，无控件被裁切 | — |

识别准确率基线（模型自带样例，`use_itn=true`）：

- 中文 `zh.wav` → `开饭时间早上9点至下午5点。`（官方真值一致）
- 英文 `en.wav` → `The tribal chieftain called for the boy and presented him with 50 pieces of code.`（官方真值一致）
- 粤语 `yue.wav` → `呢几个字都表达唔到我想讲嘅意思。`

---

## 目录结构

```text
vtw/
├─ run.pyw                    无控制台启动入口（自启与日常使用）
├─ 启动(调试).bat              带控制台的调试启动
├─ config.example.json        配置示例（复制为 config.json 后修改）
├─ requirements.txt           运行期依赖（官方预编译 wheel；许可见 THIRD_PARTY_NOTICES.md）
├─ LICENSE                    MIT
├─ README.md
├─ CONTRIBUTING.md
├─ THIRD_PARTY_NOTICES.md      第三方依赖/模型许可清单
├─ src/vtw/
│  ├─ __main__.py             命令行入口
│  ├─ app.py                  应用装配、线程模型、听写/会议/文件转写流程
│  ├─ asr.py                  SenseVoice 封装（懒加载/预热/空闲卸载）
│  ├─ audio_source.py         麦克风与系统声音采集、音量归一化
│  ├─ file_transcribe.py      音频文件解码（WAV/PyAV）与后台转写
│  ├─ meeting.py              会议转写会话与落盘
│  ├─ vad.py                  silero VAD 分段与静音裁剪
│  ├─ nativehook.py           自研 WH_KEYBOARD_LL 全局热键
│  ├─ hotkey.py               热键管理
│  ├─ inject.py / winclip.py  文字注入与剪贴板
│  ├─ overlay.py / tray.py    浮条与托盘
│  ├─ ui_windows.py           字幕/历史/关于窗口
│  ├─ settings_window.py      设置界面
│  ├─ autostart.py            开机自启注册表读写
│  ├─ punctuation.py          口述标点词 → 标点符号
│  └─ config.py / paths.py / logsetup.py / textutil.py / history.py / single.py
├─ scripts/                   setup.ps1、模型下载与各类自检脚本
├─ tests/                     unittest 单元测试（缺模型时相关用例自动跳过）
└─ 运行后生成：config.json / models/ / logs/ / output/
```

---

## 安全与隐私

- **运行期零网络请求**：不联网、不上传音频、不含遥测/账号/云同步。
  唯一的联网动作是 `scripts/download_models.py` 在安装阶段下载模型。
- 音频只在本机内存中处理；文字稿与历史记录写入项目下的 `output/`。
- 开机自启只写 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`，不需要管理员权限。

---

## 第三方组件与许可

本项目代码以 MIT 发布（见 `LICENSE`）。运行时依赖由 pip 从 PyPI 安装，仓库不打包二进制：

| 组件 | 用途 | 许可证 |
|---|---|---|
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | 识别/VAD 推理 | Apache-2.0 |
| [PyAV](https://github.com/PyAV-Org/PyAV)（含 FFmpeg） | 音频文件解码 | PyAV 源码 BSD-3-Clause；**PyPI 的二进制 wheel 内含 FFmpeg 及 x264/x265，按 GPLv2+ 或 LGPLv2.1+（含商业例外）分发**。本项目 MIT 不覆盖该 wheel，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) |
| [sounddevice](https://github.com/spatialaudio/python-sounddevice) | 麦克风采集 | MIT |
| [SoundCard](https://github.com/bastibe/SoundCard) | WASAPI 环回 | BSD-3-Clause |
| [PyAudioWPatch](https://github.com/s0d3s/PyAudioWPatch) | 环回备用后端 | Apache-2.0 |
| [soxr](https://github.com/dofuuz/python-soxr) | 重采样 | LGPL-2.1-or-later |
| [keyboard](https://github.com/boppreh/keyboard) | 热键兜底 | MIT |
| [pystray](https://github.com/moses-palmer/pystray) | 系统托盘 | LGPL-3.0 |
| [Pillow](https://python-pillow.org/) | 托盘图标绘制 | MIT-CMU |
| [numpy](https://numpy.org/) | 数值计算 | BSD-3-Clause |

> 依赖由 pip 从 PyPI 安装，本仓库**不打包**任何第三方二进制。若你计划发布 EXE、安装器或含 `.venv` 的压缩包，请先阅读 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 中 GPL/LGPL 与模型许可的说明。

**模型权重不随本仓库分发**：`scripts/download_models.py` 从 sherpa-onnx 官方 release 下载 SenseVoice int8 与 silero VAD。

- **SenseVoice 代码**：[MIT](https://github.com/FunAudioLLM/SenseVoice/blob/main/LICENSE)（Copyright 2025 FunASR）。
- **SenseVoiceSmall / 本仓库下载的 SenseVoice int8 权重**：**[FunASR Model License 1.1](https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE)**。该许可要求注明出处、保留模型名称；条款中“仅供参考/学习”的表述在商业使用时需自行评估（建议做法律审查）。
- **Silero VAD**：[MIT](https://github.com/snakers4/silero-vad/blob/master/LICENSE)（Copyright 2020-present Silero Team）。

本项目使用 **SenseVoice**（FunAudioLLM / FunASR，阿里巴巴）与 **Silero VAD**（Silero Team）；名称与版权归各自权利人。使用模型即表示遵守其上游许可。

---

## 贡献

欢迎提交 Issue / PR。开发环境、测试方式、代码约定见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## 许可证

[MIT](LICENSE) © 2026 WenquanDanDan

该 MIT 许可仅覆盖本仓库中的**源代码**；**模型权重与第三方依赖不适用 MIT**，分别遵循其自身许可证，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

---

## 免责声明

录音与转写请遵守当地法律法规及所在组织的规定，仅处理你有权录制的内容。
本项目按「现状」提供，不对识别准确率或使用后果作任何担保。
