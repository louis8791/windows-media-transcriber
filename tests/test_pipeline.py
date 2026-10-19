from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from clean_transcript import make_prompt, split_chunks, validate_response  # noqa: E402
from merge_whisper_chunks import merge_chunks, offset_timing_line  # noqa: E402
from normalize_text import convert_s2tw  # noqa: E402
from quality_report import (  # noqa: E402
    build_report,
    inspect_srt,
    repetition_findings,
)


def write_srt_pair(root: Path, cues: list[tuple[str, str, str]]) -> tuple[Path, Path]:
    """把 (start, end, text) 寫成正式版式 SRT 與對應 TXT，供品質報告測試使用。"""
    txt = root / "demo.txt"
    srt = root / "demo.srt"
    txt.write_text("\n".join(text for _, _, text in cues) + "\n", encoding="utf-8")
    srt.write_text(
        "\n\n".join(
            f"{index + 1}\n{start} --> {end}\n{text}"
            for index, (start, end, text) in enumerate(cues)
        )
        + "\n",
        encoding="utf-8",
    )
    return txt, srt


class PipelineTests(unittest.TestCase):
    def test_english_cleanup_keeps_language_and_rejects_translation(self):
        source='Dear friends thank you for joining us today. We will keep the original English text.'
        prompt=make_prompt(source,['不相關中文詞'])
        self.assertIn('ENGLISH ONLY',prompt)
        self.assertNotIn('不相關中文詞',prompt)
        self.assertIn(source,prompt)
        self.assertEqual(validate_response({'response':source},source),source)
        with self.assertRaisesRegex(RuntimeError,'翻譯'):
            validate_response({'response':'親愛的朋友們，感謝大家今天出席。'},source)

    def test_cleanup_prompt_has_no_numbered_scaffolding_and_rejects_echo(self):
        prompt = make_prompt('各位大家好。', [])
        self.assertNotIn('這是第', prompt)
        self.assertNotIn('---', prompt)
        with self.assertRaises(RuntimeError):
            validate_response({'response': '這是第 1/1 段：\n---\n各位大家好。'}, '各位大家好。')

    def test_chunking_preserves_every_character(self) -> None:
        source = "第一行\n" + ("很長的內容" * 20) + "\n最後一行"
        chunks = split_chunks(source, max_chars=19)
        self.assertEqual(source, "".join(chunks))
        self.assertTrue(all(len(chunk) <= 19 for chunk in chunks))

    def test_s2tw_uses_taiwan_terms(self) -> None:
        self.assertEqual(convert_s2tw("后台发展发型里面"), "後臺發展髮型裡面")

    def test_rejects_blank_or_thinking_response(self) -> None:
        with self.assertRaises(RuntimeError):
            validate_response({"response": "", "done_reason": "stop"}, "原文")
        with self.assertRaises(RuntimeError):
            validate_response({"response": "<think>推理</think>", "done_reason": "stop"}, "原文")

    def test_accepts_plain_qwen_response(self) -> None:
        self.assertEqual(
            validate_response({"response": "測試通過", "done_reason": "stop"}, "測試通過"),
            "測試通過",
        )

    def test_srt_report_flags_non_positive_duration(self) -> None:
        report = inspect_srt(
            "1\n00:00:00,000 --> 00:00:01,000\n正常\n\n"
            "2\n00:00:02,000 --> 00:00:02,000\n異常\n"
        )
        self.assertEqual(report, {"cue_count": 2, "invalid_timestamps": 1})

    def test_whisper_chunks_merge_and_renumber_srt(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "chunk_00000.txt").write_text("第一段\n", encoding="utf-8")
            (root / "chunk_00000.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n第一段\n", encoding="utf-8"
            )
            (root / "chunk_00000.complete").write_text("ok", encoding="ascii")
            (root / "chunk_00001.txt").write_text("第二段\n", encoding="utf-8")
            (root / "chunk_00001.srt").write_text(
                "1\n00:00:01,000 --> 00:00:02,000\n第二段\n", encoding="utf-8"
            )
            (root / "chunk_00001.complete").write_text("ok", encoding="ascii")
            output_txt = root / "merged.txt"
            output_srt = root / "merged.srt"

            merge_chunks(root, 2, output_txt, output_srt, 300)

            self.assertEqual(output_txt.read_text(encoding="utf-8"), "第一段\n第二段\n")
            self.assertEqual(
                output_srt.read_text(encoding="utf-8"),
                "1\n00:00:01,000 --> 00:00:02,000\n第一段\n\n"
                "2\n00:05:01,000 --> 00:05:02,000\n第二段\n",
            )

    def test_whisper_chunk_merge_rejects_missing_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "chunk_00000.txt").write_text("文字", encoding="utf-8")
            (root / "chunk_00000.srt").write_text("", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                merge_chunks(root, 1, root / "merged.txt", root / "merged.srt", 300)

    def test_srt_reference_format_and_timestamp_beyond_two_hours(self) -> None:
        self.assertEqual(
            offset_timing_line("00:00:01,230 --> 00:00:02,340", 7_200_000),
            "02:00:01,230 --> 02:00:02,340",
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "chunk_00000.txt").write_text("第一行 第二行\n", encoding="utf-8")
            (root / "chunk_00000.srt").write_text(
                "9\n00:00:01,230 --> 00:00:02,340\n第一行\n第二行\n",
                encoding="utf-8",
            )
            (root / "chunk_00000.complete").write_text("ok", encoding="ascii")
            output_srt = root / "merged.srt"

            merge_chunks(root, 1, root / "merged.txt", output_srt, 300)

            self.assertEqual(
                output_srt.read_text(encoding="utf-8"),
                "1\n00:00:01,230 --> 00:00:02,340\n第一行 第二行\n",
            )

    def test_quality_gate_blocks_mechanical_repetition_but_keeps_short_real_repeat(self) -> None:
        repeated = [
            {
                "number": index + 1,
                "start": f"00:00:{index * 3:02d},000",
                "end": f"00:00:{index * 3 + 3:02d},000",
                "text": "機械式重複句",
            }
            for index in range(6)
        ]
        self.assertEqual(repetition_findings(repeated)[0]["kind"], "consecutive_repetition_wall")
        self.assertEqual(
            repetition_findings(repeated[:3]),
            [],
            "真人短時間重說兩三次不可被機器審查誤判成重複牆",
        )

    def test_quality_report_blocks_frequent_long_phrase_without_deleting_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            txt = root / "demo.txt"
            srt = root / "demo.srt"
            phrase = "這是一句不應該大量出現的完整長句"
            txt.write_text((phrase + "\n") * 10, encoding="utf-8")
            srt.write_text(
                "\n\n".join(
                    f"{index + 1}\n00:00:{index:02d},000 --> 00:00:{index + 1:02d},000\n{phrase}"
                    for index in range(10)
                )
                + "\n",
                encoding="utf-8",
            )
            report = build_report(txt, srt, None)

            self.assertEqual(report["content_review_status"], "blocked")
            self.assertEqual(report["blocking_issue_count"], 1)
            self.assertEqual(txt.read_text(encoding="utf-8"), (phrase + "\n") * 10)

    def test_quality_gate_blocks_single_youtube_boilerplate_hallucination(self) -> None:
        # 上午場真實故障：cue 296 與 cue 1460 都是 YouTube 尾板幻覺，單次出現即須阻擋。
        boilerplate = "請不吝點贊 訂閱 轉發 打賞支持明鏡與點點欄目"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            txt, srt = write_srt_pair(
                root,
                [
                    ("00:19:58,630", "00:20:00,030", "導致人民犯罪"),
                    ("00:20:00,000", "00:20:31,990", boilerplate),
                    ("00:20:32,010", "00:20:38,550", "在後續發生的事件中負責調查的部門是特別調查組。"),
                    ("01:14:57,650", "01:14:59,090", "在12分鐘總33"),
                    ("01:14:59,090", "01:14:59,360", boilerplate),
                ],
            )
            report = build_report(txt, srt, None)

            hallucinations = [
                item for item in report["review_findings"]
                if item["kind"] == "hallucination_boilerplate"
            ]
            self.assertEqual(len(hallucinations), 2)
            self.assertTrue(all(item["severity"] == "blocking" for item in hallucinations))
            # 0.27 秒那條無法靠時長規則抓到，只能靠樣板詞庫。
            self.assertEqual(hallucinations[1]["start"], "01:14:59,090")
            self.assertEqual(report["content_review_status"], "blocked")

    def test_quality_gate_blocks_overlong_single_cue(self) -> None:
        # 下午場真實故障：單條 cue 掛 59.1 秒。
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            txt, srt = write_srt_pair(
                root,
                [
                    ("01:46:50,670", "01:46:52,900", "阿彌陀佛"),
                    ("01:46:52,900", "01:47:52,000", "修行人將善念善行"),
                ],
            )
            report = build_report(txt, srt, None)

            overlong = [item for item in report["review_findings"] if item["kind"] == "overlong_cue"]
            self.assertEqual(len(overlong), 1)
            self.assertEqual(overlong[0]["severity"], "blocking")
            self.assertAlmostEqual(overlong[0]["duration_seconds"], 59.1, places=3)
            self.assertEqual(report["content_review_status"], "blocked")
            # 同一條 cue 不得同時再產生 low_speech_rate，避免報告洗版。
            self.assertEqual(
                [item for item in report["review_findings"] if item["kind"] == "low_speech_rate"],
                [],
            )

    def test_quality_gate_does_not_block_normal_chinese_transcript(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            sentences = [
                "各位貴賓大家早安",
                "今天我們要討論的是人權保障",
                "這個案件已經延續了十九年",
                "行政程序必須符合正當法律程序",
                "感謝各位專家學者出席",
                "接下來請下一位與談人發言",
            ]
            cues = [
                (
                    f"00:00:{index * 4:02d},000",
                    f"00:00:{index * 4 + 4:02d},000",
                    sentence,
                )
                for index, sentence in enumerate(sentences)
            ]
            txt, srt = write_srt_pair(root, cues)
            report = build_report(txt, srt, None)

            self.assertEqual(report["review_findings"], [])
            self.assertEqual(report["blocking_issue_count"], 0)
            self.assertEqual(report["content_review_status"], "owner_review_required")
            self.assertTrue(report["owner_content_review_required"])

    def test_minor_boundary_overlap_is_counted_but_not_reported(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            txt, srt = write_srt_pair(
                root,
                [
                    ("00:04:59,000", "00:05:00,030", "分段前的最後一句"),
                    ("00:05:00,000", "00:05:02,000", "分段後的第一句"),
                ],
            )
            report = build_report(txt, srt, None)

            self.assertEqual(report["minor_overlap_count"], 1)
            self.assertEqual(
                [item for item in report["review_findings"] if item["kind"] == "cue_overlap"],
                [],
            )

    def test_english_segment_is_listed_without_changing_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            txt, srt = write_srt_pair(
                root,
                [
                    ("00:00:00,000", "00:00:03,000", "以下是外賓致詞"),
                    ("00:00:03,000", "00:00:07,000", "Thank you very much for the invitation."),
                    ("00:00:07,000", "00:00:11,000", "Human rights protection remains unfinished."),
                    ("00:00:11,000", "00:00:15,000", "I would like to thank the organisers."),
                    ("00:00:15,000", "00:00:18,000", "謝謝這位貴賓的發言"),
                ],
            )
            report = build_report(txt, srt, None)

            self.assertEqual(len(report["english_segments"]), 1)
            self.assertEqual(report["english_segments"][0]["cue_start"], 2)
            self.assertEqual(report["english_segments"][0]["cue_end"], 4)
            self.assertEqual(report["english_segments"][0]["cue_count"], 3)
            self.assertEqual(report["content_review_status"], "owner_review_required")
            self.assertEqual(report["blocking_issue_count"], 0)


if __name__ == "__main__":
    unittest.main()
