from __future__ import annotations

import argparse
import os
import re
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def chunk_paths(chunk_dir: Path, expected_count: int) -> list[tuple[Path, Path]]:
    if expected_count < 1:
        raise ValueError("expected_count 必須至少為 1")
    chunks: list[tuple[Path, Path]] = []
    for index in range(expected_count):
        base = chunk_dir / f"chunk_{index:05d}"
        marker = base.with_suffix(".complete")
        txt = base.with_suffix(".txt")
        srt = base.with_suffix(".srt")
        missing = [path.name for path in (marker, txt, srt) if not path.exists()]
        if missing:
            raise RuntimeError(f"分段 {index + 1} 尚未完成：{', '.join(missing)}")
        chunks.append((txt, srt))
    return chunks


def merge_txt_files(paths: list[Path]) -> str:
    parts = [path.read_text(encoding="utf-8-sig").rstrip("\r\n") for path in paths]
    return "\n".join(part for part in parts if part) + ("\n" if any(parts) else "")


_TIMESTAMP_RE = re.compile(
    r"^(?P<h>\d{2}):(?P<m>\d{2}):(?P<s>\d{2}),(?P<ms>\d{3})$"
)


def parse_timestamp(value: str) -> int:
    match = _TIMESTAMP_RE.match(value.strip())
    if not match:
        raise RuntimeError(f"無效 SRT 時間碼：{value}")
    return (
        int(match.group("h")) * 3_600_000
        + int(match.group("m")) * 60_000
        + int(match.group("s")) * 1_000
        + int(match.group("ms"))
    )


def format_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def offset_timing_line(line: str, offset_ms: int) -> str:
    try:
        start, end = [part.strip() for part in line.split("-->", maxsplit=1)]
    except ValueError as error:
        raise RuntimeError(f"無效 SRT cue：{line}") from error
    return (
        f"{format_timestamp(parse_timestamp(start) + offset_ms)} --> "
        f"{format_timestamp(parse_timestamp(end) + offset_ms)}"
    )


def merge_srt_files(paths: list[Path], chunk_seconds: int) -> str:
    merged: list[str] = []
    cue_number = 1
    for chunk_index, path in enumerate(paths):
        content = path.read_text(encoding="utf-8-sig").strip()
        if not content:
            continue
        for block in re.split(r"\r?\n\s*\r?\n", content):
            lines = block.splitlines()
            if len(lines) < 3 or not lines[0].strip().isdigit() or "-->" not in lines[1]:
                raise RuntimeError(f"SRT 分段格式錯誤：{path.name}")
            timing = offset_timing_line(lines[1], chunk_index * chunk_seconds * 1_000)
            body = " ".join(line.strip() for line in lines[2:] if line.strip())
            if not body:
                raise RuntimeError(f"SRT 分段字幕為空：{path.name}")
            merged.append("\n".join([str(cue_number), timing, body]))
            cue_number += 1
    return "\n\n".join(merged) + ("\n" if merged else "")


def merge_chunks(
    chunk_dir: Path,
    expected_count: int,
    output_txt: Path,
    output_srt: Path,
    chunk_seconds: int,
) -> None:
    chunks = chunk_paths(chunk_dir, expected_count)
    atomic_write_text(output_txt, merge_txt_files([txt for txt, _ in chunks]))
    atomic_write_text(
        output_srt, merge_srt_files([srt for _, srt in chunks], chunk_seconds)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="合併已完成的 Whisper 分段輸出")
    parser.add_argument("chunk_dir", type=Path)
    parser.add_argument("expected_count", type=int)
    parser.add_argument("output_txt", type=Path)
    parser.add_argument("output_srt", type=Path)
    parser.add_argument("chunk_seconds", type=int)
    args = parser.parse_args()
    merge_chunks(
        args.chunk_dir,
        args.expected_count,
        args.output_txt,
        args.output_srt,
        args.chunk_seconds,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
