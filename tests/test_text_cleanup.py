"""识别结果文本清洗的单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vtw.textutil import (  # noqa: E402
    clean_text,
    is_meaningful,
    normalize_whitespace,
    restore_english_case,
    strip_control_tags,
)


class TestControlTags(unittest.TestCase):
    def test_strips_sense_voice_tags(self) -> None:
        raw = "<|zh|><|NEUTRAL|><|Speech|><|withitn|>今天天气不错"
        self.assertEqual(strip_control_tags(raw), "今天天气不错")

    def test_strips_tags_anywhere(self) -> None:
        raw = "前面<|en|>中间<|HAPPY|>后面"
        self.assertEqual(strip_control_tags(raw), "前面中间后面")

    def test_strips_special_tokens(self) -> None:
        self.assertEqual(strip_control_tags("[blank]你好[noise]"), "你好")

    def test_empty_input(self) -> None:
        self.assertEqual(strip_control_tags(""), "")


class TestWhitespace(unittest.TestCase):
    def test_collapses_inner_spaces(self) -> None:
        self.assertEqual(normalize_whitespace("今天   天气  不错"), "今天 天气 不错")

    def test_strips_leading_trailing(self) -> None:
        self.assertEqual(normalize_whitespace("  你好  "), "你好")

    def test_handles_fullwidth_space(self) -> None:
        self.assertEqual(normalize_whitespace("你好\u3000世界"), "你好 世界")

    def test_limits_blank_lines(self) -> None:
        self.assertEqual(normalize_whitespace("a\n\n\n\nb"), "a\n\nb")

    def test_crlf_normalized(self) -> None:
        self.assertEqual(normalize_whitespace("a\r\nb"), "a\nb")


class TestCleanText(unittest.TestCase):
    def test_english_case_preserved_by_default(self) -> None:
        # 这是本项目最重要的一条：官方示例的 .lower() 会破坏英文大小写
        self.assertEqual(clean_text("Hello World"), "Hello World")

    def test_lowercase_only_when_requested(self) -> None:
        self.assertEqual(clean_text("Hello World", lowercase=True), "hello world")

    def test_trailing_space_optional(self) -> None:
        self.assertEqual(clean_text("你好", trailing_space=True), "你好 ")
        self.assertEqual(clean_text("你好"), "你好")

    def test_no_double_trailing_space(self) -> None:
        self.assertEqual(clean_text("你好 ", trailing_space=True), "你好 ")

    def test_empty_stays_empty(self) -> None:
        self.assertEqual(clean_text(""), "")
        self.assertEqual(clean_text("   "), "")

    def test_combined_tags_and_spaces(self) -> None:
        raw = "<|zh|><|NEUTRAL|><|Speech|>  今天  天气 不错 <|withitn|>"
        self.assertEqual(clean_text(raw), "今天 天气 不错")

    def test_chinese_punctuation_untouched(self) -> None:
        text = "你好，世界！"
        self.assertEqual(clean_text(text), text)


class TestIsMeaningful(unittest.TestCase):
    def test_real_text_is_meaningful(self) -> None:
        self.assertTrue(is_meaningful("你好"))
        self.assertTrue(is_meaningful("hello"))

    def test_empty_is_not_meaningful(self) -> None:
        self.assertFalse(is_meaningful(""))
        self.assertFalse(is_meaningful("   "))

    def test_punctuation_only_is_not_meaningful(self) -> None:
        # 静音时模型偶尔会吐出孤立的标点，不应触发注入
        self.assertFalse(is_meaningful("。"))
        self.assertFalse(is_meaningful("..."))
        self.assertFalse(is_meaningful("，。！"))

    def test_mixed_punctuation_and_digits(self) -> None:
        self.assertTrue(is_meaningful("3.14"))


#: SenseVoice 在真实中文样例上的输出形态（含控制标签与多余空格）
_REAL_SAMPLE = "<|zh|><|NEUTRAL|><|Speech|> 开饭时间早上九点至下午五点 <|withitn|>"


class TestEnglishSentenceCase(unittest.TestCase):
    """SenseVoice 会把整句英文输出成全大写，需要保守还原。"""

    def test_all_caps_sentence_is_restored(self) -> None:
        raw = "THE TRIVBAL CHIEF THIN CALLED FOR THE BOY"
        self.assertEqual(restore_english_case(raw), "The trivbal chief thin called for the boy")

    def test_multiple_sentences(self) -> None:
        raw = "HELLO WORLD. THIS IS A TEST! HOW ARE YOU?"
        self.assertEqual(restore_english_case(raw), "Hello world. This is a test! How are you?")

    def test_acronyms_preserved(self) -> None:
        raw = "DON'T WORRY, I WILL USE THE API AND THE GPU"
        self.assertEqual(restore_english_case(raw), "Don't worry, I will use the API and the GPU")

    def test_first_person_i_preserved(self) -> None:
        self.assertEqual(restore_english_case("I AM FINE"), "I am fine")

    def test_normal_cased_text_untouched(self) -> None:
        raw = "The tribal chieftain called for the boy"
        self.assertEqual(restore_english_case(raw), raw)

    def test_chinese_with_english_acronym_untouched(self) -> None:
        raw = "用 API 调用 GPU 很快"
        self.assertEqual(restore_english_case(raw), raw)

    def test_pure_chinese_untouched(self) -> None:
        self.assertEqual(restore_english_case("你好世界"), "你好世界")

    def test_short_acronyms_not_rewritten(self) -> None:
        # 字母少于 3 个时不做任何处理，避免误伤 "OK" "ID" 这类输入
        self.assertEqual(restore_english_case("OK"), "OK")

    def test_empty_input(self) -> None:
        self.assertEqual(restore_english_case(""), "")

    def test_clean_text_applies_restoration(self) -> None:
        self.assertEqual(
            clean_text("HELLO WORLD"), "Hello world"
        )

    def test_clean_text_can_disable_restoration(self) -> None:
        self.assertEqual(
            clean_text("HELLO WORLD", english_sentence_case=False), "HELLO WORLD"
        )


class TestRealSampleShape(unittest.TestCase):
    def test_full_pipeline_on_realistic_output(self) -> None:
        cleaned = clean_text(_REAL_SAMPLE)
        self.assertEqual(cleaned, "开饭时间早上九点至下午五点")
        self.assertTrue(is_meaningful(cleaned))


if __name__ == "__main__":
    unittest.main()
