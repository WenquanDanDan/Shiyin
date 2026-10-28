"""口述标点转换的单元测试。

测试用例里那条长字符串是**真实模型输出**：用 Windows 自带 TTS
（Microsoft Huihui, zh-CN）合成"大家好逗号今天天气不错句号…"再跑识别得到的，
不是手写臆造的输入。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw.punctuation import (  # noqa: E402
    apply_spoken_punctuation,
    contains_spoken_punctuation,
)
from vtw.textutil import clean_text  # noqa: E402

#: 真实识别输出（TTS 合成的口述标点音频）
REAL_MODEL_OUTPUT = "大家好，逗号，今天天气不错，句号，请问你叫什么名字？问号感叹号换行新的异行。"
REAL_EXPECTED = "大家好，今天天气不错。请问你叫什么名字？！\n新的异行。"


class TestChinesePunctuation(unittest.TestCase):
    def test_real_model_output(self) -> None:
        """核心场景：模型把标点词当普通词转写，且前后还补了自动标点。"""
        self.assertEqual(apply_spoken_punctuation(REAL_MODEL_OUTPUT), REAL_EXPECTED)

    def test_simple_sentence(self) -> None:
        self.assertEqual(apply_spoken_punctuation("你好逗号世界句号"), "你好，世界。")

    def test_enumeration_comma(self) -> None:
        self.assertEqual(
            apply_spoken_punctuation("第一点顿号第二点顿号第三点"), "第一点、第二点、第三点"
        )

    def test_quotes_and_colon(self) -> None:
        self.assertEqual(
            apply_spoken_punctuation("他说冒号左引号你好右引号"), "他说：\u201c你好\u201d"
        )

    def test_newline_variants(self) -> None:
        self.assertEqual(apply_spoken_punctuation("上面换行下面"), "上面\n下面")
        self.assertEqual(apply_spoken_punctuation("上面换段下面"), "上面\n\n下面")

    def test_question_and_exclamation(self) -> None:
        self.assertEqual(apply_spoken_punctuation("你好吗问号"), "你好吗？")
        self.assertEqual(apply_spoken_punctuation("太好了感叹号"), "太好了！")

    def test_ellipsis_and_dash_not_collapsed(self) -> None:
        result = apply_spoken_punctuation("他说省略号")
        self.assertEqual(result, "他说……")
        self.assertEqual(apply_spoken_punctuation("前面破折号后面"), "前面——后面")

    def test_no_spoken_punctuation_returns_unchanged(self) -> None:
        text = "今天天气不错"
        self.assertEqual(apply_spoken_punctuation(text), text)

    def test_empty_input(self) -> None:
        self.assertEqual(apply_spoken_punctuation(""), "")

    def test_does_not_create_double_punctuation(self) -> None:
        """模型补的自动标点必须被吃掉，不能出现「大家好，，，今天」。"""
        result = apply_spoken_punctuation("大家好，逗号，今天")
        self.assertNotIn("，，", result)
        self.assertEqual(result, "大家好，今天")


class TestEnglishPunctuation(unittest.TestCase):
    def test_comma_and_period_casing(self) -> None:
        self.assertEqual(apply_spoken_punctuation("HELLO COMMA WORLD PERIOD"), "HELLO, WORLD.")

    def test_lowercase_input(self) -> None:
        self.assertEqual(apply_spoken_punctuation("hello comma world period"), "hello, world.")

    def test_question_mark_multiword(self) -> None:
        self.assertEqual(apply_spoken_punctuation("what is this question mark"), "what is this?")

    def test_new_line_removes_leading_space(self) -> None:
        self.assertEqual(
            apply_spoken_punctuation("first line new line second line"),
            "first line\nsecond line",
        )

    def test_parentheses_hug_content(self) -> None:
        self.assertEqual(
            apply_spoken_punctuation("open parenthesis hello close parenthesis"), "(hello)"
        )

    def test_no_trailing_space_before_punctuation(self) -> None:
        """英文输出不能出现 "HELLO , WORLD" 这种逗号前带空格的结果。"""
        result = apply_spoken_punctuation("HELLO COMMA WORLD")
        self.assertNotIn(" ,", result)
        self.assertNotIn(" .", result)


class TestKnownAmbiguity(unittest.TestCase):
    """中文无词间空格导致的固有限制，明确记录行为而不是假装不存在。"""

    def test_discussing_punctuation_is_also_converted(self) -> None:
        # 这是已知代价：说"这个逗号用法不对"会被改成"这个，用法不对"
        self.assertEqual(apply_spoken_punctuation("这个逗号用法不对"), "这个，用法不对")

    def test_disabling_chinese_leaves_text_alone(self) -> None:
        text = "这个逗号用法不对"
        self.assertEqual(apply_spoken_punctuation(text, chinese=False), text)


class TestDetection(unittest.TestCase):
    def test_detects_spoken_punctuation(self) -> None:
        self.assertTrue(contains_spoken_punctuation("你好逗号世界"))
        self.assertTrue(contains_spoken_punctuation("HELLO COMMA WORLD"))

    def test_plain_text_not_detected(self) -> None:
        self.assertFalse(contains_spoken_punctuation("今天天气不错"))


class TestPipelineIntegration(unittest.TestCase):
    """通过 clean_text 走完整清洗链路。"""

    def test_clean_text_applies_spoken_punctuation(self) -> None:
        self.assertEqual(clean_text("你好逗号世界句号"), "你好，世界。")

    def test_can_be_disabled(self) -> None:
        self.assertEqual(
            clean_text("你好逗号世界句号", spoken_punctuation=False), "你好逗号世界句号"
        )

    def test_control_tags_stripped_before_conversion(self) -> None:
        raw = "<|zh|><|NEUTRAL|><|Speech|>你好逗号世界句号<|withitn|>"
        self.assertEqual(clean_text(raw), "你好，世界。")

    def test_real_model_output_through_pipeline(self) -> None:
        self.assertEqual(clean_text(REAL_MODEL_OUTPUT), REAL_EXPECTED)


if __name__ == "__main__":
    unittest.main()
