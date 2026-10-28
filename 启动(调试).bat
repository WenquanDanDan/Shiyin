@echo off
rem vtw 调试启动：带控制台窗口，可以看到实时日志。
rem 日常使用请双击 run.pyw（无控制台）或依赖开机自启。
cd /d "%~dp0"
".venv\Scripts\python.exe" -m vtw -v %*
pause
