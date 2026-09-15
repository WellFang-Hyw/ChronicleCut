@echo off
REM ============================================================
REM  historical_story_gen 一键入口
REM
REM    run.bat                     随机题材，出横屏+竖屏两版
REM    run.bat -t "赤壁之战"        指定题材
REM    run.bat --minutes 9         目标 9 分钟
REM    run.bat plan                只写稿，不出语音和视频
REM    run.bat probe-tts           实测语速（换音色后校准）
REM    run.bat probe-images        检查图源通不通
REM    run.bat voices              列出 MiniMax 音色
REM    run.bat smoke               零 LLM 的媒体链路冒烟测试（静态图路线）
REM    run.bat smoke-clips         零 API 冒烟：影视切片 + EDL 剪辑链路
REM
REM    run.bat agent               看生产线卡在哪、下一步该干什么 ← 切片路线入口
REM    run.bat agent --stage 1 -t "题材"  出脚本 + 分镜 + 素材需求清单（然后停下等你剪素材）
REM    run.bat agent --stage 2           AI 排镜头(EDL) + 渲染 + 自检，出成片
REM    run.bat test                零成本回归测试
REM    run.bat history             看生成记录（已做过哪些故事）
REM    run.bat history --backfill  把 data\output 下已有 metadata 补录进记录
REM
REM  日志同时在屏幕上和 data\output\ 下的 log 文件里。
REM ============================================================
setlocal
cd /d "%~dp0"

REM Hermes 等环境会注入 uv 的 PYTHONHOME，会让别的 Python 解释器崩在
REM "AssertionError: SRE module mismatch"。清掉它再跑。
set PYTHONHOME=
set UV_INTERNAL__PYTHONHOME=
set PYTHONUTF8=1

if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  echo [!] 没找到 .venv。首次使用请先执行：
  echo     python -m venv .venv
  echo     .venv\Scripts\python.exe -m pip install -e .
  echo ^(依赖清单在 pyproject.toml；免费兜底语音要额外装：pip install edge-tts^)
  set "PY=python"
)

if "%1"=="" (
  echo === 随机题材，开始生成 ===
) else (
  echo === 参数: %* ===
)

"%PY%" -u run.py %*
set EXITCODE=%ERRORLEVEL%
echo.
echo === 结束，退出码 %EXITCODE% ===
if "%EXITCODE%"=="0" (
  echo 成片在 data\output\ 下。
)
endlocal & exit /b %EXITCODE%
