from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from opencc import OpenCC


def convert_s2tw(text: str) -> str:
    return OpenCC("s2tw").convert(text)


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


def normalize_file(source: Path, destination: Path) -> None:
    atomic_write_text(destination, convert_s2tw(source.read_text(encoding="utf-8-sig")))


def main() -> int:
    parser = argparse.ArgumentParser(description="以 OpenCC s2tw 產生臺灣正體中文檔案")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    normalize_file(args.source, args.destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
