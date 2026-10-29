# 贡献指南

感谢你愿意改进**拾音/Shiyin**。这个项目只支持 **Windows**，目标是保持「完全离线、零遥测、开箱即用」，提交前请先确认改动不破坏这一点。
这本身就是一个vibecoding项目，欢迎使用各路AI狠狠加以改进。

## 注
由于VC时的文件夹名是vtw，这里面可能会偶尔出现这个名字，请见谅，当然帮忙改掉更是欢迎。

## 开发环境

- Windows 10/11 x64
- Python **3.14 x64**
- Git

```powershell
git clone <your-fork-url>
cd vtw
powershell -ExecutionPolicy Bypass -File scripts\setup.ps1 -SkipAutostart
```

`-SkipAutostart` 只安装依赖和模型，不写开机自启；开发时建议加上。

## 运行

```powershell
# 带控制台日志
.\.venv\Scripts\python.exe -m vtw -v

# 无控制台（与日常使用一致）
.\.venv\Scripts\python.exe run.pyw
```

## 测试

```powershell
# 单元测试；未下载模型时相关用例会自动跳过
.\.venv\Scripts\python.exe -m unittest discover -s tests

# 需要真实麦克风/扬声器/桌面的自检脚本
.\.venv\Scripts\python.exe scripts\smoke_asr.py
.\.venv\Scripts\python.exe scripts\test_hotkey.py
.\.venv\Scripts\python.exe scripts\test_injection.py
.\.venv\Scripts\python.exe scripts\test_meeting_gui.py
.\.venv\Scripts\python.exe scripts\test_file_transcribe.py
```

`scripts\test_settings_gui.py` 会临时修改本机热键与 `config.json`，结束时会还原；
请勿在正在使用的情况下运行它。

## 代码约定

- 注释、docstring、用户可见提示使用中文；变量名/函数名使用英文。
- **运行期不得联网**。只有 `scripts/download_models.py` 在安装阶段下载模型；
  不要加入遥测、自动更新、云端识别。
- 不提交 `models/`、`output/`、`logs/`、`config.json`、`.venv/`。
- `.ps1` 必须保存为 **UTF-8 with BOM + CRLF**，否则 Windows PowerShell 5.1
  会按 ANSI(GBK) 解码，中文注释会导致语法错误。
- 新增第三方依赖时：更新 `requirements.txt`、说明用途，并确认许可证与
  「官方预编译 wheel、无需编译器」的约束一致；同时更新
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)（GPL/LGPL 依赖尤其要注明分发义务）。
- 修改音频/识别逻辑时，请在 PR 里附上测试方式与前后对比（RTF、识别文本）。

## 提交 PR

1. 从 `main` 切分支：`git checkout -b fix/xxx`
2. 保证 `python -m unittest discover -s tests` 通过（模型缺失时的 skip 属正常）。
3. 提交信息用中文或英文均可。

## 许可证

提交即表示你同意以本项目的 MIT License 发布你的贡献（见 `LICENSE`）。
