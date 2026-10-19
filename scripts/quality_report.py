from __future__ import annotations

import argparse
from collections import defaultdict
import json
import re
from pathlib import Path


TIMESTAMP = re.compile(
    r"^(\d{2}):(\d{2}):(\d{2}),(\d{3})\s+-->\s+(\d{2}):(\d{2}):(\d{2}),(\d{3})$"
)
SRT_BLOCK = re.compile(
    r"^(\d+)\n(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n(.+)$"
)

# 幻覺樣板詞庫；`data/hallucination_blocklist.txt` 不存在時使用這份內建預設。
DEFAULT_HALLUCINATION_PATTERNS = (
    "請不吝點贊",
    "點贊",
    "訂閱",
    "轉發",
    "打賞",
    "明鏡",
    "點點欄目",
    "字幕志願者",
    "字幕由.*提供",
    "謝謝觀看",
    "謝謝收看",
    "請訂閱我的頻道",
)
DEFAULT_BLOCKLIST_PATH = Path(__file__).resolve().parents[1] / "data" / "hallucination_blocklist.txt"

# 判定門檻；集中在此避免散落在各偵測函式。
HALLUCINATION_RESIDUAL_BLOCK_CHARS = 8
OVERLONG_CUE_BLOCK_MS = 25_000
OVERLONG_CUE_WARN_MS = 15_000
LOW_SPEECH_RATE_MIN_MS = 10_000
LOW_SPEECH_RATE_CHARS_PER_SECOND = 0.6
CUE_OVERLAP_WARN_MS = 500
LONG_GAP_MS = 20_000
ENGLISH_ALPHA_RATIO = 0.6
ENGLISH_SEGMENT_MIN_CUES = 3


def timestamp_ms(parts: tuple[str, ...]) -> int:
    hours, minutes, seconds, millis = map(int, parts)
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def inspect_srt(text: str) -> dict[str, int]:
    cue_count = 0
    invalid_timestamps = 0
    for line in text.splitlines():
        match = TIMESTAMP.match(line.strip())
        if not match:
            continue
        cue_count += 1
        if timestamp_ms(match.groups()[:4]) >= timestamp_ms(match.groups()[4:]):
            invalid_timestamps += 1
    return {"cue_count": cue_count, "invalid_timestamps": invalid_timestamps}


def parse_cues(text: str) -> list[dict]:
    cues = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        match = SRT_BLOCK.match(block.replace("\r\n", "\n"))
        if not match:
            continue
        cues.append(
            {
                "number": int(match.group(1)),
                "start": match.group(2),
                "end": match.group(3),
                "text": match.group(4).strip(),
            }
        )
    return cues


def normalized_repetition_text(text: str) -> str:
    return re.sub(r"[\W_]+", "", text, flags=re.UNICODE)


def cue_ms(value: str) -> int:
    """把 `HH:MM:SS,mmm` 轉成毫秒。"""
    return timestamp_ms(tuple(value.replace(",", ":").split(":")))


def repetition_findings(cues: list[dict]) -> list[dict]:
    findings = []
    covered_texts = set()
    start = 0
    while start < len(cues):
        normalized = normalized_repetition_text(cues[start]["text"])
        end = start + 1
        while end < len(cues) and normalized and normalized_repetition_text(cues[end]["text"]) == normalized:
            end += 1
        run = cues[start:end]
        duration_ms = timestamp_ms(tuple(run[-1]["end"].replace(",", ":").split(":"))) - timestamp_ms(
            tuple(run[0]["start"].replace(",", ":").split(":"))
        )
        if len(normalized) >= 4 and len(run) >= 6 and duration_ms >= 15_000:
            covered_texts.add(normalized)
            findings.append(
                {
                    "severity": "blocking",
                    "kind": "consecutive_repetition_wall",
                    "cue_start": run[0]["number"],
                    "cue_end": run[-1]["number"],
                    "start": run[0]["start"],
                    "end": run[-1]["end"],
                    "count": len(run),
                    "text": run[0]["text"],
                }
            )
        start = end

    occurrences: dict[str, list[dict]] = defaultdict(list)
    for cue in cues:
        normalized = normalized_repetition_text(cue["text"])
        if len(normalized) >= 8:
            occurrences[normalized].append(cue)
    for normalized, repeated in occurrences.items():
        if normalized in covered_texts or len(repeated) < 6:
            continue
        findings.append(
            {
                "severity": "blocking" if len(repeated) >= 10 else "warning",
                "kind": "frequent_exact_phrase",
                "cue_start": repeated[0]["number"],
                "cue_end": repeated[-1]["number"],
                "start": repeated[0]["start"],
                "end": repeated[-1]["end"],
                "count": len(repeated),
                "text": repeated[0]["text"],
            }
        )
    return findings


def load_hallucination_patterns(path: Path | None = None) -> list[str]:
    """讀取幻覺樣板詞庫；檔案不存在或全是註解時回到內建預設。"""
    source = DEFAULT_BLOCKLIST_PATH if path is None else path
    if not source.exists():
        return list(DEFAULT_HALLUCINATION_PATTERNS)
    patterns = []
    for line in source.read_text(encoding="utf-8-sig").splitlines():
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        patterns.append(entry)
    return patterns or list(DEFAULT_HALLUCINATION_PATTERNS)


def compile_patterns(patterns: list[str]) -> list[tuple[str, re.Pattern[str]]]:
    """詞庫允許 regex 或純字串；不合法的 regex 退回字面比對，不讓整份報告失敗。"""
    compiled = []
    for entry in patterns:
        try:
            compiled.append((entry, re.compile(entry)))
        except re.error:
            compiled.append((entry, re.compile(re.escape(entry))))
    return compiled


def hallucination_findings(cues: list[dict], patterns: list[str] | None = None) -> list[dict]:
    """比對整條 cue；命中樣板詞且剩餘內容極少即單次阻擋。"""
    compiled = compile_patterns(patterns if patterns is not None else load_hallucination_patterns())
    findings = []
    for cue in cues:
        text = cue["text"]
        covered: set[int] = set()
        matched: list[str] = []
        for entry, regex in compiled:
            hit = False
            for match in regex.finditer(text):
                if match.end() <= match.start():
                    continue
                covered.update(range(match.start(), match.end()))
                hit = True
            if hit:
                matched.append(entry)
        if not matched:
            continue
        residual = sum(
            1
            for index, char in enumerate(text)
            if index not in covered and not char.isspace()
        )
        findings.append(
            {
                "severity": "blocking" if residual < HALLUCINATION_RESIDUAL_BLOCK_CHARS else "warning",
                "kind": "hallucination_boilerplate",
                "cue_start": cue["number"],
                "cue_end": cue["number"],
                "start": cue["start"],
                "end": cue["end"],
                "count": 1,
                "matched": matched,
                "residual_chars": residual,
                "text": text,
            }
        )
    return findings


def timing_analysis(cues: list[dict]) -> dict:
    """單條 cue 時長、語速、cue 重疊與長空白；空白只列清單不影響狀態。"""
    findings: list[dict] = []
    minor_overlap_count = 0
    long_gaps: list[dict] = []
    previous: dict | None = None
    for cue in cues:
        start_ms = cue_ms(cue["start"])
        end_ms = cue_ms(cue["end"])
        duration_ms = end_ms - start_ms
        effective_chars = len(normalized_repetition_text(cue["text"]))
        base = {
            "cue_start": cue["number"],
            "cue_end": cue["number"],
            "start": cue["start"],
            "end": cue["end"],
            "count": 1,
            "duration_seconds": round(duration_ms / 1000.0, 3),
            "text": cue["text"],
        }
        overlong = duration_ms >= OVERLONG_CUE_WARN_MS
        if overlong:
            findings.append(
                {
                    "severity": "blocking" if duration_ms >= OVERLONG_CUE_BLOCK_MS else "warning",
                    "kind": "overlong_cue",
                    **base,
                }
            )
        elif duration_ms >= LOW_SPEECH_RATE_MIN_MS and duration_ms > 0:
            # 與 overlong_cue 命中同一條 cue 時只留較嚴重的那筆，避免報告洗版。
            rate = effective_chars / (duration_ms / 1000.0)
            if rate < LOW_SPEECH_RATE_CHARS_PER_SECOND:
                findings.append(
                    {
                        "severity": "warning",
                        "kind": "low_speech_rate",
                        "chars_per_second": round(rate, 3),
                        "effective_chars": effective_chars,
                        **base,
                    }
                )
        if previous is not None:
            previous_end_ms = cue_ms(previous["end"])
            overlap_ms = previous_end_ms - start_ms
            if overlap_ms > 0:
                if overlap_ms >= CUE_OVERLAP_WARN_MS:
                    findings.append(
                        {
                            "severity": "warning",
                            "kind": "cue_overlap",
                            "cue_start": previous["number"],
                            "cue_end": cue["number"],
                            "start": cue["start"],
                            "end": previous["end"],
                            "count": 1,
                            "overlap_seconds": round(overlap_ms / 1000.0, 3),
                            "text": cue["text"],
                        }
                    )
                else:
                    minor_overlap_count += 1
            elif -overlap_ms >= LONG_GAP_MS:
                long_gaps.append(
                    {
                        "cue_start": previous["number"],
                        "cue_end": cue["number"],
                        "start": previous["end"],
                        "end": cue["start"],
                        "gap_seconds": round(-overlap_ms / 1000.0, 3),
                    }
                )
        previous = cue
    return {
        "findings": findings,
        "minor_overlap_count": minor_overlap_count,
        "long_gaps": long_gaps,
    }


def english_ratio(text: str) -> float:
    visible = [char for char in text if not char.isspace()]
    if not visible:
        return 0.0
    letters = sum(1 for char in visible if char.isascii() and char.isalpha())
    return letters / len(visible)


def english_segments(cues: list[dict]) -> list[dict]:
    """連續英文 cue 區段；只提示可能需要以 -l en 重轉，不影響狀態。"""
    segments: list[dict] = []
    run: list[dict] = []
    for cue in cues + [None]:
        if cue is not None and english_ratio(cue["text"]) > ENGLISH_ALPHA_RATIO:
            run.append(cue)
            continue
        if len(run) >= ENGLISH_SEGMENT_MIN_CUES:
            segments.append(
                {
                    "cue_start": run[0]["number"],
                    "cue_end": run[-1]["number"],
                    "start": run[0]["start"],
                    "end": run[-1]["end"],
                    "cue_count": len(run),
                }
            )
        run = []
    return segments


def review_findings(cues: list[dict], patterns: list[str] | None = None) -> list[dict]:
    """彙整所有阻擋／警告型疑點；依 cue 位置排序方便人工對照原音。"""
    timing = timing_analysis(cues)
    findings = repetition_findings(cues) + hallucination_findings(cues, patterns) + timing["findings"]
    findings.sort(key=lambda item: (item["cue_start"], item["cue_end"], item["kind"]))
    return findings


def finding_description(finding: dict) -> str:
    kind = finding["kind"]
    if kind == "consecutive_repetition_wall":
        return "同一句連續重複 {0} 次".format(finding["count"])
    if kind == "frequent_exact_phrase":
        return "同一完整長句全檔出現 {0} 次".format(finding["count"])
    if kind == "hallucination_boilerplate":
        return "命中幻覺樣板詞（{0}），剩餘非命中內容 {1} 字".format(
            "、".join(finding.get("matched", [])), finding.get("residual_chars", 0)
        )
    if kind == "overlong_cue":
        return "單條 cue 長達 {0:.2f} 秒".format(finding.get("duration_seconds", 0))
    if kind == "low_speech_rate":
        return "cue 長 {0:.2f} 秒但每秒只有 {1:.2f} 個有效字".format(
            finding.get("duration_seconds", 0), finding.get("chars_per_second", 0)
        )
    if kind == "cue_overlap":
        return "與前一條 cue 重疊 {0:.2f} 秒".format(finding.get("overlap_seconds", 0))
    return kind


def review_text(report: dict) -> str:
    blocked = report["content_review_status"] == "blocked"
    lines = [
        "逐字稿機器審查：" + ("未通過" if blocked else "未發現阻擋型疑點"),
        "機器只標記疑點，沒有自動刪改逐字稿；內容仍需人工對照原音。",
        "",
    ]
    if not report["review_findings"]:
        lines.append("未偵測到重複牆、幻覺樣板詞或異常 cue 時長。")
    for finding in report["review_findings"]:
        label = "阻擋" if finding["severity"] == "blocking" else "警告"
        lines.append(
            f"- [{label}] {finding['start']}–{finding['end']}，cue {finding['cue_start']}–{finding['cue_end']}，"
            f"{finding_description(finding)}：{finding['text']}"
        )
    if report.get("minor_overlap_count"):
        lines.append("")
        lines.append(
            f"分段邊界微幅重疊（小於 0.5 秒）共 {report['minor_overlap_count']} 處，屬正常現象，未列成疑點。"
        )
    if report.get("long_gaps"):
        lines.append("")
        lines.append("長時間無字幕區段（僅供人工抽查，不影響審查狀態）：")
        for gap in report["long_gaps"]:
            lines.append(
                f"- {gap['start']}–{gap['end']}，cue {gap['cue_start']}–{gap['cue_end']}，"
                f"間隔 {gap['gap_seconds']:.2f} 秒"
            )
    if report.get("english_segments"):
        lines.append("")
        lines.append("疑似英文發言區段（僅供人工抽查，不影響審查狀態；必要時可用 -l en 重轉）：")
        for segment in report["english_segments"]:
            lines.append(
                f"- {segment['start']}–{segment['end']}，cue {segment['cue_start']}–{segment['cue_end']}，"
                f"共 {segment['cue_count']} 條"
            )
    return "\n".join(lines) + "\n"


def build_report(
    txt: Path, srt: Path, cleaned: Path | None, blocklist: Path | None = None
) -> dict:
    txt_text = txt.read_text(encoding="utf-8-sig")
    srt_text = srt.read_text(encoding="utf-8-sig")
    cues = parse_cues(srt_text)
    patterns = load_hallucination_patterns(blocklist)
    findings = review_findings(cues, patterns)
    timing = timing_analysis(cues)
    blocking_count = sum(item["severity"] == "blocking" for item in findings)
    return {
        "txt_path": str(txt),
        "srt_path": str(srt),
        "txt_chars": len(txt_text),
        "txt_nonempty_lines": sum(bool(line.strip()) for line in txt_text.splitlines()),
        **inspect_srt(srt_text),
        "cleaned_path": str(cleaned) if cleaned else None,
        "cleaned_exists": bool(cleaned and cleaned.exists()),
        "content_review_status": "blocked" if blocking_count else "owner_review_required",
        "blocking_issue_count": blocking_count,
        "warning_issue_count": len(findings) - blocking_count,
        "review_findings": findings,
        "minor_overlap_count": timing["minor_overlap_count"],
        "long_gaps": timing["long_gaps"],
        "english_segments": english_segments(cues),
        "owner_content_review_required": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="輸出逐字稿結構檢查 JSON")
    parser.add_argument("txt", type=Path)
    parser.add_argument("srt", type=Path)
    parser.add_argument("--cleaned", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-output", type=Path)
    parser.add_argument("--blocklist", type=Path, help="幻覺樣板詞庫；預設 data/hallucination_blocklist.txt")
    args = parser.parse_args()
    report = build_report(args.txt, args.srt, args.cleaned, args.blocklist)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.review_output:
        args.review_output.parent.mkdir(parents=True, exist_ok=True)
        args.review_output.write_text(review_text(report), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
