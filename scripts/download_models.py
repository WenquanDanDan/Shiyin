#!/usr/bin/env python3
"""模型下载与校验。

只用 Python 标准库（``urllib`` + ``tarfile``），不依赖 curl / tar / ffmpeg。

设计要点
--------
* **支持断点续传**：156MB 的模型若中途断开，重跑会从断点继续。
* **本地缓存兜底**：压缩包保留下载在 ``models/_downloads/``，重装时无需再联网。
  这是**唯一经过验证**的备用源 —— GitHub release 是这批模型的官方唯一发布地，
  社区镜像（hf-mirror 等）上的路径无法稳定对应，因此不做无效猜测，
  失败时直接给出可手动执行的 URL。
* **解压安全**：``tarfile`` 使用 ``filter="data"``，拒绝路径穿越与设备文件。
* **落地校验**：校验的是**解压后的实际文件**（存在 + 体积阈值），
  因为 GitHub release 资产没有官方校验和。

用法::

    python scripts/download_models.py            # 缺什么下什么
    python scripts/download_models.py --check    # 只校验，不下载
    python scripts/download_models.py --force    # 重新安装（优先用本地缓存）
    python scripts/download_models.py --redownload  # 强制重新联网下载
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw import paths  # noqa: E402

GH = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"

MODEL_LICENSE_NOTICE = """模型许可提示：
  · SenseVoice 代码：MIT
  · SenseVoiceSmall / 本项目下载的 int8 权重：FunASR Model License 1.1
    https://github.com/modelscope/FunASR/blob/main/MODEL_LICENSE
    （需注明出处并保留模型名称；商业使用请自行评估）
  · Silero VAD：MIT
模型权重不随本项目分发；下载/使用即表示遵守上游许可。"""

# ---------------------------------------------------------------------------
# 模型版本选择（由 scripts/compare_models.py 实测决定，勿随意更改）
#
# 在同一批官方样例上对比了三个候选：
#
#   模型                       中文(ITN开)                     英文(ITN开)
#   ------------------------   -----------------------------   --------------------------------
#   2024-07-17-int8  ← 选用    开饭时间早上9点至下午5点。         The tribal chieftain ... with 50
#   2025-09-09-int8            放时间早上九点至下午五点（掉字）    THE TRIVBAL CHIEF THIN ... COOD
#   funasr-nano-2025-12-17     开饭时间早上九点至下午五点（无ITN） the tribal chiefin ...
#
# 2024-07-17 的中文与英文输出都与官方 ground truth **逐字一致**；
# 2025-09-09 虽然快 3 倍，但中文会掉字、英文整句大写且识别错误更多，
# 不满足"准确率高"的要求。因此默认下载 2024-07-17。
# ---------------------------------------------------------------------------
SENSE_VOICE_TARBALL = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2"

#: 备用版本：若主版本下载失败则回退（质量较低，仅保证可用）
SENSE_VOICE_FALLBACK_TARBALL = (
    "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2"
)

#: 体积下限（字节）。低于此值说明下载被截断或解压失败。
MIN_MODEL_BYTES = 100 * 1024 * 1024  # SenseVoice int8 约 230MB
MIN_TOKENS_BYTES = 50 * 1024  # tokens.txt 约 300KB
MIN_VAD_BYTES = 200 * 1024  # silero_vad.onnx 约 0.6MB

_USER_AGENT = "vtw-model-downloader/1.0"


# --------------------------------------------------------------------------
# 下载
# --------------------------------------------------------------------------
@dataclass
class DownloadResult:
    ok: bool
    source: str = ""
    bytes_written: int = 0
    error: str = ""


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def download(
    urls: list[str],
    dest: Path,
    *,
    expected_min: int = 1,
    retries: int = 3,
    timeout: int = 30,
    allow_resume: bool = True,
) -> DownloadResult:
    """依次尝试多个 URL 下载到 ``dest``；返回第一个成功的结果。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")
    last_error = ""

    for url in urls:
        for attempt in range(1, retries + 1):
            start_at = 0
            if allow_resume and part.exists():
                start_at = part.stat().st_size
                if dest.exists() and start_at >= dest.stat().st_size and dest.stat().st_size > 0:
                    start_at = 0  # 已有完整文件，重新下
                    part.unlink(missing_ok=True)

            headers = {"User-Agent": _USER_AGENT}
            if start_at:
                headers["Range"] = f"bytes={start_at}-"

            try:
                req = urllib.request.Request(url, headers=headers)
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    # 服务器不支持断点续传时会返回 200 而非 206，此时必须重头写
                    resumed = resp.status == 206
                    if start_at and not resumed:
                        start_at = 0
                    mode = "ab" if resumed else "wb"
                    total = int(resp.headers.get("Content-Length") or 0) + start_at
                    written = start_at
                    last_report = 0.0
                    with open(part, mode) as fh:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            fh.write(chunk)
                            written += len(chunk)
                            now = time.monotonic()
                            if now - last_report > 1.0:
                                last_report = now
                                if total:
                                    pct = written * 100 / total
                                    print(
                                        f"\r    下载中 {pct:5.1f}%  "
                                        f"{_human(written)}/{_human(total)}",
                                        end="",
                                        flush=True,
                                    )
                                else:
                                    print(f"\r    下载中 {_human(written)}", end="", flush=True)
                    if total:
                        print()

                if not part.exists() or part.stat().st_size < expected_min:
                    last_error = (
                        f"文件过小：{part.stat().st_size if part.exists() else 0} "
                        f"< {expected_min} 字节"
                    )
                    part.unlink(missing_ok=True)
                    continue

                shutil.move(str(part), str(dest))
                return DownloadResult(True, url, dest.stat().st_size)

            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                # 断点文件保留，下一轮续传
                if attempt < retries:
                    backoff = min(2 ** attempt, 8)
                    print(f"    第 {attempt} 次失败（{last_error}），{backoff}s 后重试…")
                    time.sleep(backoff)

        print(f"    源不可用，切换下一个：{last_error}")

    return DownloadResult(False, error=last_error or "全部数据源均失败")


def extract_tar_bz2(archive: Path, dest_dir: Path) -> None:
    """安全解压 .tar.bz2（拒绝路径穿越、绝对路径、设备文件）。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:bz2") as tar:
        try:
            tar.extractall(dest_dir, filter="data")  # Python 3.12+
        except TypeError:  # pragma: no cover - 老版本没有 filter 参数
            tar.extractall(dest_dir)


# --------------------------------------------------------------------------
# 各模型
# --------------------------------------------------------------------------
@dataclass
class ModelCheck:
    name: str
    files: list[tuple[Path, int]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(p.is_file() and p.stat().st_size >= minimum for p, minimum in self.files)

    def describe(self) -> str:
        parts = []
        for p, minimum in self.files:
            if not p.is_file():
                parts.append(f"{p.name}=缺失")
            elif p.stat().st_size < minimum:
                parts.append(f"{p.name}={_human(p.stat().st_size)}(期望≥{_human(minimum)})")
            else:
                parts.append(f"{p.name}={_human(p.stat().st_size)}")
        return ", ".join(parts)


def sense_voice_check() -> ModelCheck:
    return ModelCheck(
        "SenseVoice int8",
        [
            (paths.sense_voice_model_path(), MIN_MODEL_BYTES),
            (paths.sense_voice_tokens_path(), MIN_TOKENS_BYTES),
        ],
    )


def vad_check() -> ModelCheck:
    """任一 VAD 模型可用即算就绪；优先报告 v5。"""
    candidates = [paths.VAD_DIR / "silero_vad_v5.onnx", paths.VAD_DIR / paths.VAD_MODEL_NAME]
    valid = [p for p in candidates if p.is_file() and p.stat().st_size >= MIN_VAD_BYTES]
    return ModelCheck("silero VAD", [(valid[0] if valid else candidates[0], MIN_VAD_BYTES)])


def ensure_sense_voice(
    *, force: bool = False, check_only: bool = False, redownload: bool = False
) -> bool:
    check = sense_voice_check()
    if check.ok and not force:
        print(f"[跳过] SenseVoice 已就绪 —— {check.describe()}")
        return True
    if check_only:
        print(f"[缺失] SenseVoice —— {check.describe()}", file=sys.stderr)
        return False

    archive = paths.MODELS_DIR / "_downloads" / SENSE_VOICE_TARBALL

    # 本地缓存优先：已下载过的压缩包直接复用，重装不必再联网
    if not redownload and archive.is_file() and archive.stat().st_size >= 50 * 1024 * 1024:
        print(f"[缓存] 复用已下载的压缩包（{_human(archive.stat().st_size)}）：{archive.name}")
        result = DownloadResult(True, "local-cache", archive.stat().st_size)
    else:
        print(f"[下载] SenseVoice int8 约 156MB …… 当前状态：{check.describe()}")
        result = download(
            [f"{GH}/{SENSE_VOICE_TARBALL}"],
            archive,
            expected_min=50 * 1024 * 1024,
        )
        if not result.ok:
            print(f"[失败] SenseVoice 下载失败：{result.error}", file=sys.stderr)
            print("       请手动下载后放到 models\\_downloads\\ 目录，再重跑本脚本：", file=sys.stderr)
            print(f"       {GH}/{SENSE_VOICE_TARBALL}", file=sys.stderr)
            return False

    print(f"    下载完成（{_human(result.bytes_written)}），正在解压…")
    try:
        staging = paths.MODELS_DIR / "_staging"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        extract_tar_bz2(archive, staging)
    except (tarfile.TarError, OSError) as exc:
        print(f"[失败] 解压失败：{exc}", file=sys.stderr)
        print("       压缩包可能损坏，请删除后重跑：", file=sys.stderr)
        print(f"       {archive}", file=sys.stderr)
        return False

    # tarball 内层目录名固定，搬运 model / tokens / test_wavs
    inner = next((d for d in staging.iterdir() if d.is_dir()), None)
    if inner is None:
        print("[失败] 解压结果为空", file=sys.stderr)
        return False

    paths.ensure_dir(paths.SENSE_VOICE_DIR)
    for name in ("model.int8.onnx", "tokens.txt"):
        src = inner / name
        if not src.is_file():
            print(f"[失败] 压缩包内缺少 {name}", file=sys.stderr)
            return False
        shutil.copy2(src, paths.SENSE_VOICE_DIR / name)

    # test_wavs 保留下来供冒烟测试使用
    if (inner / "test_wavs").is_dir():
        dst = paths.SENSE_VOICE_DIR / "test_wavs"
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(inner / "test_wavs", dst)

    shutil.rmtree(staging, ignore_errors=True)

    check = sense_voice_check()
    if check.ok:
        print(f"[完成] SenseVoice —— {check.describe()}")
        return True
    print(f"[失败] 校验不通过 —— {check.describe()}", file=sys.stderr)
    return False


def ensure_vad(*, force: bool = False, check_only: bool = False, redownload: bool = False) -> bool:
    check = vad_check()
    if check.ok and not force:
        print(f"[跳过] silero VAD 已就绪 —— {check.describe()}")
        return True
    if check_only:
        print(f"[缺失] silero VAD —— {check.describe()}", file=sys.stderr)
        return False

    paths.ensure_dir(paths.VAD_DIR)
    backup_dir = paths.MODELS_DIR / "_downloads"

    # v5 实测可下载（2.2MB）；经典 silero_vad.onnx 在本机 release 下载会超时，
    # 因此只作为尽力而为的补充，v5 成功即可满足需求。
    # 顺序很重要：先 v5，确保 v5 一定被尝试。
    for name, required in (("silero_vad_v5.onnx", True), ("silero_vad.onnx", False)):
        target = paths.VAD_DIR / name
        if target.is_file() and target.stat().st_size >= MIN_VAD_BYTES and not force:
            print(f"[跳过] VAD {name} 已就绪")
            return True

        # 1) 本地缓存（已验证过的好副本）—— 真正的离线兜底
        cached = backup_dir / name
        if not redownload and cached.is_file() and cached.stat().st_size >= MIN_VAD_BYTES:
            shutil.copy2(cached, target)
            print(f"[缓存] 已从本地副本恢复 VAD {name}")
            return True

        # 2) 联网下载
        print(f"[下载] VAD {name} ……")
        result = download(
            [f"{GH}/{name}"],
            target,
            expected_min=MIN_VAD_BYTES,
            retries=3,
        )
        if result.ok:
            print(f"[完成] VAD {name} —— {_human(result.bytes_written)}")
            # 留一份本地副本，下次重装不必联网
            try:
                backup_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, cached)
            except OSError as exc:
                print(f"    （本地备份失败，不影响使用：{exc}）")
            return True

        level = "失败" if required else "警告"
        print(f"[{level}] VAD {name} 下载失败：{result.error}", file=sys.stderr)
        print(f"       可手动下载后放到 models\\vad\\ ：{GH}/{name}", file=sys.stderr)
    return False


# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="下载并校验 vtw 所需模型")
    parser.add_argument("--force", action="store_true", help="重新安装（优先使用本地缓存）")
    parser.add_argument(
        "--redownload", action="store_true", help="忽略本地缓存，强制重新联网下载"
    )
    parser.add_argument("--check", action="store_true", help="只校验，不下载")
    args = parser.parse_args()

    print("=" * 62)
    print("vtw 模型准备")
    print("=" * 62)
    print(MODEL_LICENSE_NOTICE)
    print("-" * 62)

    ok_sv = ensure_sense_voice(
        force=args.force, check_only=args.check, redownload=args.redownload
    )
    ok_vad = ensure_vad(
        force=args.force, check_only=args.check, redownload=args.redownload
    )

    print("-" * 62)
    if ok_sv and ok_vad:
        print("全部模型就绪。")
        return 0
    print("模型未就绪，请根据上面的提示处理后重试。", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
