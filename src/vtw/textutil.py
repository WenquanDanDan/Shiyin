"""识别结果文本清洗。

从官方示例里发现的一个陷阱：``python-api-examples/offline-sense-voice-ctc-decode-files.py``
对结果做了 ``.lower()``（那是英文示例的写法）。我们**默认不做小写化**，
否则英文听写会把 "Hello World" 变成 "hello world"。
"""

from __future__ import annotations

import re
import unicodedata

#: SenseVoice 的控制标签，例如 ``<|zh|>`` ``<|NEUTRAL|>`` ``<|Speech|>`` ``<|withitn|>``。
#: 正常情况下这些标签只出现在 result.lang / result.emotion / result.event 里，
#: 但不同模型版本可能把它们混进 text，这里做防御性剥离。
_CONTROL_TAG_RE = re.compile(r"<\|[^|>]*\|>")

#: 其它可能漏出的特殊 token
_SPECIAL_TOKEN_RE = re.compile(r"\[(?:blank|unk|sil|noise|music|applause|laughter)\]", re.I)

#: 连续空白（含全角空格）
_WS_RE = re.compile(r"[ \t\u3000]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")


def strip_control_tags(text: str) -> str:
    """剥离 SenseVoice 控制标签与特殊 token。"""
    if not text:
        return ""
    out = _CONTROL_TAG_RE.sub("", text)
    out = _SPECIAL_TOKEN_RE.sub("", out)
    return out


def normalize_whitespace(text: str) -> str:
    """压缩空白：行内多空格合一，去首尾，限制连续空行。"""
    if not text:
        return ""
    lines = [_WS_RE.sub(" ", line).strip() for line in text.replace("\r\n", "\n").split("\n")]
    out = "\n".join(lines)
    out = _MULTI_NEWLINE_RE.sub("\n\n", out)
    return out.strip()


def clean_text(
    text: str,
    *,
    lowercase: bool = False,
    trailing_space: bool = False,
    english_sentence_case: bool = True,
    spoken_punctuation: bool = True,
) -> str:
    """识别结果的统一清洗入口。

    :param text: ``stream.result.text`` 原文。
    :param lowercase: 是否转小写。默认 ``False`` —— 保住英文大小写。
    :param trailing_space: 是否在末尾补一个空格（连续听写时方便接续）。
    :param english_sentence_case: 纯大写英文句子是否还原成正常大小写。
        见 :func:`restore_english_case`。
    :param spoken_punctuation: 是否把口述的标点词（"逗号"、"句号"、"换行"）转成符号。
        见 :mod:`vtw.punctuation`。
    """
    if not text:
        return ""
    out = strip_control_tags(text)
    if english_sentence_case:
        # 先还原大小写：此时还保留着模型原始的大小写形态，判断更准
        out = restore_english_case(out)
    if spoken_punctuation:
        from .punctuation import apply_spoken_punctuation

        out = apply_spoken_punctuation(out)
    out = normalize_whitespace(out)
    if lowercase:
        out = out.lower()
    out = unicodedata.normalize("NFC", out)
    if trailing_space and out and not out.endswith((" ", "\n")):
        out += " "
    return out


def is_meaningful(text: str, *, min_chars: int = 1) -> bool:
    """判断识别结果是否值得注入（过滤静音/纯噪声产生的空结果或标点碎片）。"""
    if not text:
        return False
    stripped = text.strip()
    if len(stripped) < min_chars:
        return False
    # 只剩标点/符号也视为无意义
    return any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in stripped)


# ---------------------------------------------------------------------------
# 英文大小写还原
# ---------------------------------------------------------------------------
#: 应保持全大写的常见缩写（否则会被句子化规则改成小写）
_ACRONYMS = {
    "AI", "API", "ASR", "BIOS", "CDN", "CEO", "CPU", "CSS", "CTO", "DB", "DNS",
    "EXE", "FAQ", "FPS", "GB", "GPU", "GPT", "GUI", "HDD", "HDMI", "HTML", "HTTP",
    "HTTPS", "ID", "IDE", "IM", "IO", "IP", "IT", "JSON", "KB", "LAN", "LLM",
    "MB", "NLP", "OCR", "OK", "OS", "PDF", "PHP", "PID", "RAM", "REST", "SDK",
    "SQL", "SSD", "SSH", "SSL", "TCP", "TLS", "TTS", "UDP", "UI", "UHD", "UK",
    "URL", "USB", "USA", "UX", "VPN", "WAN", "WIFI", "XML", "YAML", "ZIP",
}

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
_ALPHA_RE = re.compile(r"[A-Za-z]")
_UPPER_RE = re.compile(r"[A-Z]")
#: 句子边界：. ! ? 以及中文句末标点，后跟空白
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。！？])\s+")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z']*")


def _looks_like_all_caps_english(text: str) -> bool:
    """判断是否是"整句被模型转成大写"的纯英文片段。

    条件：不含 CJK、至少有 3 个字母、且**所有**字母都是大写。
    这样中文夹英文缩写（如"用 API 调用"）不会被误判。
    """
    if _CJK_RE.search(text):
        return False
    letters = _ALPHA_RE.findall(text)
    if len(letters) < 3:
        return False
    return len(_UPPER_RE.findall(text)) == len(letters)


def _sentence_case_word(word: str, *, first_in_sentence: bool) -> str:
    upper = word.upper()
    if upper in _ACRONYMS:
        return upper
    if upper in {"I", "I'M", "I'VE", "I'LL", "I'D"}:
        # 第一人称代词永远大写
        return "I" + word[1:].lower() if len(word) > 1 else "I"
    lowered = word.lower()
    if first_in_sentence:
        return lowered[:1].upper() + lowered[1:]
    return lowered


def restore_english_case(text: str) -> str:
    """把整句大写的英文还原成正常大小写。

    背景：SenseVoice 对英文常直接输出全大写（例如
    ``THE TRIVBAL CHIEF THIN CALLED FOR THE BOY``），而模型本身不提供大小写信息。
    官方示例的做法是 ``.lower()``（全部转小写），但那会连句首大写一起丢掉。
    这里做**保守的句子化**：句首大写、其余小写、常见缩写保留大写。

    只有在"整段没有任何小写字母、且不含中日韩字符"时才会生效，
    因此不会破坏正常输出，也不会把 ``用 API 调用`` 里的缩写改掉。
    """
    if not text or not _looks_like_all_caps_english(text):
        return text

    sentences = _SENTENCE_SPLIT_RE.split(text)
    rebuilt: list[str] = []
    for sentence in sentences:
        if not sentence:
            continue
        seen_first = False

        def _replace(match: re.Match[str]) -> str:
            nonlocal seen_first
            word = match.group(0)
            result = _sentence_case_word(word, first_in_sentence=not seen_first)
            if not seen_first:
                # 缩写在句首时仍然大写，句首标记照常消费
                seen_first = True
            return result

        rebuilt.append(_WORD_RE.sub(_replace, sentence))

    # 用原有分隔符重新拼接，保持标点与空白不变
    return _rejoin(text, rebuilt)


def _rejoin(original: str, rebuilt: list[str]) -> str:
    """按原分隔符把句子拼回去，避免改变空白结构。"""
    separators = _SENTENCE_SPLIT_RE.findall(original)
    out = ""
    for index, sentence in enumerate(rebuilt):
        out += sentence
        if index < len(separators):
            out += separators[index]
    return out
