"""路径解析。

所有路径都相对项目根目录（即 ``D:\\program\\vtw``），保证工具自包含、可整体搬迁。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# src/vtw/paths.py -> src/vtw -> src -> <root>
PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]

SRC_DIR: Path = PROJECT_ROOT / "src"
MODELS_DIR: Path = PROJECT_ROOT / "models"
SENSE_VOICE_DIR: Path = MODELS_DIR / "sense-voice"
VAD_DIR: Path = MODELS_DIR / "vad"
LOGS_DIR: Path = PROJECT_ROOT / "logs"

#: SenseVoice 主模型文件名（int8 量化，约 158MB 解压后约 230MB）
SENSE_VOICE_MODEL_NAME: str = "model.int8.onnx"
SENSE_VOICE_TOKENS_NAME: str = "tokens.txt"

#: silero VAD 模型（约 0.6MB）
VAD_MODEL_NAME: str = "silero_vad.onnx"

CONFIG_FILE: Path = PROJECT_ROOT / "config.json"
LOG_FILE: Path = LOGS_DIR / "vtw.log"
HISTORY_FILE: Path = PROJECT_ROOT / "output" / "history.jsonl"

#: 有控制台的运行方式（python.exe）vs 无控制台的运行方式（pythonw.exe）
IS_WINDOWED: bool = Path(sys.executable).name.lower().startswith("pythonw")


def ensure_dir(path: Path) -> Path:
    """创建目录（含父目录）并返回它，已存在则直接返回。"""
    path.mkdir(parents=True, exist_ok=True)
    return path


def resolve_output_dir(configured: str | os.PathLike[str] | None) -> Path:
    """把配置里的 output_dir 解析成绝对路径。

    相对路径按项目根解析；为空则回落到 ``<root>/output``。
    """
    if not configured:
        return ensure_dir(PROJECT_ROOT / "output")
    p = Path(configured)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return ensure_dir(p)


def sense_voice_model_path() -> Path:
    return SENSE_VOICE_DIR / SENSE_VOICE_MODEL_NAME


def sense_voice_tokens_path() -> Path:
    return SENSE_VOICE_DIR / SENSE_VOICE_TOKENS_NAME


def vad_model_path() -> Path:
    return VAD_DIR / VAD_MODEL_NAME


def as_uri_arg(path: Path) -> str:
    """转成子进程命令行参数（带引号包裹，避免空格问题）。"""
    return f'"{path}"'
