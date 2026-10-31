"""配置读写。

设计要点：
* 单一 JSON 文件（``config.json``），人可读可手改。
* 缺失字段用默认值补齐，未知字段原样保留（向前兼容）。
* 文件损坏时**不崩溃**：备份为 ``config.json.bad``，用默认值重建。
"""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path
from typing import Any

from . import paths

log = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    # ---- 热键 ----
    "hotkey_dictate": "ctrl+alt+space",
    "hotkey_meeting_toggle": "ctrl+alt+a",
    "mode": "push_to_talk",  # push_to_talk | toggle
    # ---- 音频 ----
    "input_device": "default",
    "loopback_device": "default",
    "sample_rate": 16000,
    "min_dictation_seconds": 0.35,
    "max_dictation_seconds": 120,
    # ---- 识别 ----
    "language": "auto",  # auto | zh | en | ja | ko | yue
    "use_itn": True,  # 数字/标点归一化
    "lowercase_output": False,  # 保持英文大小写
    "english_sentence_case": True,  # 整句大写的英文还原为正常大小写
    "spoken_punctuation": True,  # 口述标点词转成标点符号（"逗号"→"，"）
    "num_threads": 4,
    "preload_model": True,
    "idle_unload_seconds": 0,  # >0 时空闲卸载模型以回收内存
    "warmup_delay_seconds": 8,  # 推迟预热，避免拖慢登录
    # ---- 输出 ----
    "inject_method": "clipboard",  # clipboard | unicode
    "restore_clipboard": True,
    "clipboard_restore_delay_ms": 400,
    "trailing_space": False,
    "output_dir": "output",
    "history_enabled": True,
    "save_meeting_audio": False,
    # ---- 启动 ----
    # 以管理员身份运行：全局键盘钩子在非提权进程里可能收不到按键
    # （UIPI 限制 + 部分安全软件拦截），默认开启
    "run_as_admin": True,
    # ---- 界面 ----
    "overlay_enabled": True,
    "overlay_position": "bottom-center",
    "show_notifications": True,
    # ---- 会议 ----
    "meeting_source": "loopback",  # loopback（系统声音）| mic（麦克风）| both（两者）
    "vad": {
        "threshold": 0.5,
        "min_silence_duration": 0.5,
        "min_speech_duration": 0.25,
        "max_speech_duration": 30.0,
    },
}

_VALID_MODES = {"push_to_talk", "toggle"}
_VALID_INJECT = {"clipboard", "unicode"}
_VALID_LANGUAGES = {"auto", "zh", "en", "ja", "ko", "yue"}
_VALID_OVERLAY_POS = {"bottom-center", "top-center", "bottom-right", "off"}
_VALID_MEETING_SOURCES = {"loopback", "mic", "both"}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """把 override 递归合并进 base 的副本；override 中的未知键也保留。"""
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _clamp(value: Any, low: float, high: float, fallback: float) -> float:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, num))


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """校验并修正配置值；非法值回落到默认值，不抛异常。"""
    cfg = _deep_merge(DEFAULTS, raw)

    if cfg.get("mode") not in _VALID_MODES:
        log.warning("配置 mode=%r 非法，回退 %s", cfg.get("mode"), DEFAULTS["mode"])
        cfg["mode"] = DEFAULTS["mode"]

    if cfg.get("inject_method") not in _VALID_INJECT:
        log.warning("配置 inject_method=%r 非法，回退 clipboard", cfg.get("inject_method"))
        cfg["inject_method"] = DEFAULTS["inject_method"]

    if cfg.get("language") not in _VALID_LANGUAGES:
        log.warning("配置 language=%r 非法，回退 auto", cfg.get("language"))
        cfg["language"] = "auto"

    if cfg.get("overlay_position") not in _VALID_OVERLAY_POS:
        cfg["overlay_position"] = DEFAULTS["overlay_position"]

    if cfg.get("meeting_source") not in _VALID_MEETING_SOURCES:
        log.warning(
            "配置 meeting_source=%r 非法，回退 %s",
            cfg.get("meeting_source"),
            DEFAULTS["meeting_source"],
        )
        cfg["meeting_source"] = DEFAULTS["meeting_source"]

    cfg["num_threads"] = int(_clamp(cfg.get("num_threads"), 1, 16, DEFAULTS["num_threads"]))
    cfg["sample_rate"] = int(_clamp(cfg.get("sample_rate"), 8000, 48000, 16000))
    cfg["max_dictation_seconds"] = _clamp(
        cfg.get("max_dictation_seconds"), 5, 3600, DEFAULTS["max_dictation_seconds"]
    )
    cfg["min_dictation_seconds"] = _clamp(
        cfg.get("min_dictation_seconds"), 0.1, 5, DEFAULTS["min_dictation_seconds"]
    )
    cfg["clipboard_restore_delay_ms"] = int(
        _clamp(cfg.get("clipboard_restore_delay_ms"), 50, 3000, 400)
    )
    cfg["idle_unload_seconds"] = int(
        _clamp(cfg.get("idle_unload_seconds"), 0, 86400, 0)
    )
    cfg["warmup_delay_seconds"] = _clamp(cfg.get("warmup_delay_seconds"), 0, 600, 8)

    vad = cfg.setdefault("vad", {})
    vad["threshold"] = _clamp(vad.get("threshold"), 0.01, 0.99, 0.5)
    vad["min_silence_duration"] = _clamp(vad.get("min_silence_duration"), 0.05, 10, 0.5)
    vad["min_speech_duration"] = _clamp(vad.get("min_speech_duration"), 0.05, 10, 0.25)
    vad["max_speech_duration"] = _clamp(vad.get("max_speech_duration"), 1, 120, 30.0)

    # 布尔字段强制转为真布尔（JSON 里可能被写成字符串）
    for key in (
        "use_itn",
        "lowercase_output",
        "english_sentence_case",
        "spoken_punctuation",
        "preload_model",
        "restore_clipboard",
        "trailing_space",
        "history_enabled",
        "save_meeting_audio",
        "overlay_enabled",
        "show_notifications",
    ):
        cfg[key] = bool(cfg.get(key, DEFAULTS.get(key, False)))

    return cfg


def _missing_keys(raw: dict[str, Any]) -> list[str]:
    """找出配置文件中缺失的顶层默认键（递归检查一层嵌套字典）。"""
    missing: list[str] = []
    for key, default in DEFAULTS.items():
        if key not in raw:
            missing.append(key)
        elif isinstance(default, dict) and isinstance(raw.get(key), dict):
            for sub in default:
                if sub not in raw[key]:
                    missing.append(f"{key}.{sub}")
    return missing


class Config:
    """配置对象。支持 ``cfg["key"]`` 与 ``cfg.key`` 两种读取方式。"""

    def __init__(self, data: dict[str, Any] | None = None, path: Path | None = None) -> None:
        self.path = Path(path) if path else paths.CONFIG_FILE
        self._data = normalize(data or {})

    # -- 读写 -------------------------------------------------------------
    @classmethod
    def load(cls, path: Path | None = None, *, create_if_missing: bool = True) -> "Config":
        target = Path(path) if path else paths.CONFIG_FILE
        raw: dict[str, Any] = {}
        if target.is_file():
            try:
                text = target.read_text(encoding="utf-8")
                loaded = json.loads(text) if text.strip() else {}
                if not isinstance(loaded, dict):
                    raise ValueError(f"配置根节点应为对象，实际为 {type(loaded).__name__}")
                raw = loaded
            except Exception as exc:  # noqa: BLE001 - 任何损坏都要能恢复
                log.error("配置文件损坏（%s），已备份并重建默认配置", exc)
                backup = target.with_suffix(".json.bad")
                try:
                    target.replace(backup)
                except OSError:
                    pass
                raw = {}
        cfg = cls(raw, target)

        # 旧版本写的配置文件会缺少新增项。运行时有默认值兜底，
        # 但为了让 config.json 始终自解释（用户手改时能看见所有选项），
        # 这里把补齐后的配置写回一次。已存在的值不受影响，且是幂等的。
        missing = _missing_keys(raw)
        if create_if_missing and (not target.is_file() or missing):
            if missing:
                log.info("配置文件缺少 %d 个新项，已补齐：%s", len(missing), ", ".join(missing))
            cfg.save()
        return cfg

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            tmp.replace(self.path)  # 原子替换，避免写一半崩溃留下坏文件
        except OSError as exc:
            log.error("配置保存失败: %s", exc)

    # -- 访问 -------------------------------------------------------------
    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value

    def update(self, **kwargs: Any) -> None:
        self._data.update(kwargs)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __getattr__(self, name: str) -> Any:
        # 仅在被访问的键存在时生效，避免掩盖真实属性错误
        data = self.__dict__.get("_data")
        if isinstance(data, dict) and name in data:
            return data[name]
        raise AttributeError(name)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Config(path={self.path!r}, keys={len(self._data)})"
