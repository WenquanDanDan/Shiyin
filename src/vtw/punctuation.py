"""口述标点：把说出来的标点词转换成真正的标点符号。

背景（实测）
------------
模型自带**自动标点**（ITN）——会根据语气停顿自动补 `，` `。` `？`。
但它**不会**把说出口的标点词转成符号。实测一段"大家好逗号今天天气不错句号"
的输出是::

    大家好，逗号，今天天气不错，句号，请问你叫什么名字？问号感叹号换行新的异行。

可以看到标点词被当普通词原样转写，而且模型还在它前后补了自动标点，读起来很怪。

本模块做两件事
--------------
1. 把口述标点词替换为对应符号；
2. **吃掉紧邻的自动标点**，避免出现 `大家好，，，今天` 这种重复
   （做法：先把标点词换成不可见标记，删掉标记两侧的自动标点，再还原成符号）。

已知歧义（重要）
----------------
中文没有词间空格，因此无法区分"我要说标点"和"我在谈论标点"：

* 说"大家好**逗号**今天天气不错" → 期望 `大家好，今天天气不错`  ✅
* 说"这个**逗号**用法不对"       → 会被改成 `这个，用法不对`      ❌

这是所有语音输入法的固有问题（英文有空格可缓解，但 `period` 也有"时期"的意思）。
因此该功能可通过 ``config.json`` 的 ``spoken_punctuation`` 或设置界面关闭。
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

#: 中文口述标点。键越长越优先匹配（"左引号" 要先于 "引号"）
CHINESE_PUNCTUATION: dict[str, str] = {
    "逗号": "，",
    "句号": "。",
    "问号": "？",
    "感叹号": "！",
    "叹号": "！",
    "分号": "；",
    "冒号": "：",
    "顿号": "、",
    "省略号": "……",
    "破折号": "——",
    "左引号": "\u201c",
    "右引号": "\u201d",
    "前引号": "\u201c",
    "后引号": "\u201d",
    "左括号": "（",
    "右括号": "）",
    "前括号": "（",
    "后括号": "）",
    "书名号": "《》",
    "换行": "\n",
    "回车": "\n",
    "换段": "\n\n",
    "空格": " ",
}

#: 英文口述标点（模型输出可能全大写，匹配时不区分大小写）
ENGLISH_PUNCTUATION: dict[str, str] = {
    "exclamation mark": "!",
    "exclamation point": "!",
    "question mark": "?",
    "new paragraph": "\n\n",
    "open parenthesis": "(",
    "close parenthesis": ")",
    "full stop": ".",
    "semicolon": ";",
    "comma": ",",
    "period": ".",
    "colon": ":",
    "ellipsis": "\u2026",
    "newline": "\n",
    "new line": "\n",
    "space": " ",
}

#: 标记两侧需要被吃掉的自动标点
_ADJACENT = "，。、；：？！,.;:?!\u201c\u201d\u2018\u2019（）()《》\u2026\u2014"

#: 连续重复标点的收敛规则
_COLLAPSE_RULES: tuple[tuple[str, str], ...] = (
    ("，", "，"), ("。", "。"), ("；", "；"), ("：", "："),
    ("？", "？"), ("！", "！"), ("、", "、"),
    (",", ","), (".", "."), ("?", "?"), ("!", "!"),
)


def _build_patterns(english: bool = True) -> list[tuple[re.Pattern[str], str]]:
    table: dict[str, str] = dict(CHINESE_PUNCTUATION)
    if english:
        table.update(ENGLISH_PUNCTUATION)

    # 长词优先，避免 "左引号" 被 "引号" 抢先、"question mark" 被 "mark" 抢先
    patterns: list[tuple[re.Pattern[str], str]] = []
    for word in sorted(table, key=len, reverse=True):
        flags = re.IGNORECASE if word.isascii() else 0
        patterns.append((re.compile(re.escape(word), flags), table[word]))
    return patterns


def _strip_adjacent_auto_punctuation(text: str, token: str) -> str:
    """删掉紧挨着标记的自动标点与空白。

    左侧要连**普通空格一起吃掉**：英文输出是 ``HELLO COMMA WORLD``，
    若只删标点，替换后会变成 ``HELLO , WORLD``（逗号前多个空格）。
    右侧只吃掉"空白+标点"，保留标点后的那个空格，得到 ``HELLO, WORLD``。
    """
    escaped = re.escape(token)
    punct = re.escape(_ADJACENT)
    # 左侧：空白 + 自动标点 + 空白
    text = re.sub(r"[ \t]*[" + punct + r"]*[ \t]*(?=" + escaped + r")", "", text)
    # 右侧：空白 + 自动标点（至少一个标点）
    text = re.sub(r"(?<=" + escaped + r")[ \t]*[" + punct + r"]+", "", text)
    return text


#: 左/右配对符号：替换后应贴紧相邻文字，不能留空格
_OPENING = "（(【[《\u201c\u2018"
_CLOSING = "）)】]》\u201d\u2019"


def _tidy_spacing(text: str) -> str:
    """收拾替换后残留的空格。

    例如 ``period new line second`` 会先变成 ``.\\n second``，
    ``open parenthesis hello`` 会变成 ``( hello`` —— 都要贴紧。
    """
    text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
    text = re.sub(r"([" + re.escape(_OPENING) + r"])[ \t]+", r"\1", text)
    text = re.sub(r"[ \t]+([" + re.escape(_CLOSING) + r"])", r"\1", text)
    return text


def _collapse_duplicates(text: str) -> str:
    """把 ``，，`` 这类重复标点收敛成一个。"""
    for char, single in _COLLAPSE_RULES:
        escaped = re.escape(char)
        text = re.sub(escaped + r"{2,}", single, text)
    # 中英文混排时常见的 "，," 组合
    text = re.sub(r"([，。；：？！、])[,.;:?!]", r"\1", text)
    # 行首标点无意义
    text = re.sub(r"\n[ \t]*([，。；：、？！])", r"\1", text)
    return _tidy_spacing(text)


def apply_spoken_punctuation(
    text: str,
    *,
    chinese: bool = True,
    english: bool = True,
) -> str:
    """把文本中口述的标点词替换成真正的标点符号。

    没有任何口述标点词时原样返回（不会做多余改动）。
    """
    if not text:
        return text

    patterns = _build_patterns(english)
    if not chinese:
        patterns = [
            (p, s) for (p, s) in patterns if s not in set(CHINESE_PUNCTUATION.values())
        ]

    tokens: dict[str, str] = {}
    out = text
    for index, (pattern, symbol) in enumerate(patterns):
        if not pattern.search(out):
            continue
        token = f"\x00{index}\x00"
        tokens[token] = symbol
        out = pattern.sub(token, out)

    if not tokens:
        return text

    for token in tokens:
        out = _strip_adjacent_auto_punctuation(out, token)
    for token, symbol in tokens.items():
        out = out.replace(token, symbol)

    return _collapse_duplicates(out)


def contains_spoken_punctuation(text: str, *, english: bool = True) -> bool:
    """文本里是否出现了口述标点词（用于提示/统计）。"""
    return any(pattern.search(text) for pattern, _ in _build_patterns(english))
