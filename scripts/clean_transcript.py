from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.error
import urllib.request
from pathlib import Path

from normalize_text import atomic_write_text, convert_s2tw


THINKING_MARKERS = re.compile(r"<\/?think>|```analysis|```thinking", re.IGNORECASE)


class OutputTruncatedError(RuntimeError):
    pass


def split_chunks(text: str, max_chars: int = 5000) -> list[str]:
    if max_chars < 1:
        raise ValueError("max_chars 必須大於 0")
    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:max_chars])
            line = line[max_chars:]
        if current and len(current) + len(line) > max_chars:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    if not chunks and text:
        chunks.append(text)
    return chunks


def load_terms(path: Path | None) -> list[str]:
    if not path or not path.exists():
        return []
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def is_english_text(text: str) -> bool:
    words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", text)
    return len(words) >= 8 and len(''.join(words)) > len(text.strip()) * 0.6


def make_prompt(chunk: str, terms: list[str]) -> str:
    if is_english_text(chunk):
        return f"""/no_think
Proofread this English transcript. Return the transcript in ENGLISH ONLY.
Do not translate. Fix only obvious typos, punctuation and sentence boundaries.
Preserve the original words, meaning, order, names, numbers, negations and [unclear] markers.
Do not summarize, add missing speech, guess names or provide explanations.
If no correction is needed, return the original text unchanged.
Return only the corrected English text, without labels or Markdown.

<transcript>
{chunk}
</transcript>"""
    term_text = "、".join(terms) if terms else "（無）"
    return f"""/no_think
你是中英文逐字稿校稿員。保留原文語言：英文仍用英文，中文用臺灣繁體中文；不得翻譯。請只修正明顯錯字、標點與斷句，不摘要、不增刪事實、不改變原意或段落順序。不得猜測姓名、地名、法律術語、數字或補寫漏句；保留不確定標記及所有否定詞。專有名詞表只供參考，不能把不確定的詞強行換成表中的名稱。輸出只能包含校稿後正文，不要說明，也不要 Markdown。
專有名詞：{term_text}

{chunk}"""


def validate_response(payload: dict, source: str) -> str:
    if payload.get("done_reason") == "length":
        raise OutputTruncatedError("Qwen 輸出因長度限制遭截斷")
    response = payload.get("response")
    if not isinstance(response, str) or not response.strip():
        raise RuntimeError("Qwen 沒有回傳可用正文")
    if THINKING_MARKERS.search(response):
        raise RuntimeError("Qwen 正文混入推理標記")
    scaffolding = re.compile(r"(?m)^\s*(?:這是第\s*\d+/\d+\s*段[：:]?|---|/no_think)\s*$")
    if scaffolding.search(response) and not scaffolding.search(source):
        raise RuntimeError("Qwen 正文混入提示格式，未交付校稿候選")
    cleaned = response.strip()
    source_words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", source)
    if is_english_text(source):
        output_words = re.findall(r"[A-Za-z]+(?:'[A-Za-z]+)?", cleaned)
        if len(''.join(output_words)) < len(cleaned) * 0.5:
            raise RuntimeError('Qwen 將英文原文翻譯成其他語言，未交付校稿候選')
        negation = re.compile(r"\b(?:not|no|never|neither|nor|without|cannot|\w+n't)\b", re.I)
        if [word.casefold() for word in negation.findall(source)] != [word.casefold() for word in negation.findall(cleaned)]:
            raise RuntimeError('Qwen 改變英文否定詞，未交付校稿候選')
    if sorted(re.findall(r'\d+(?:[.,]\d+)*', source)) != sorted(re.findall(r'\d+(?:[.,]\d+)*', cleaned)):
        raise RuntimeError('Qwen 改變數字，未交付校稿候選')
    if re.findall(r'\[(?:待確認|unclear)[^\]]*\]', source, re.I) != re.findall(r'\[(?:待確認|unclear)[^\]]*\]', cleaned, re.I):
        raise RuntimeError('Qwen 改變待確認標記，未交付校稿候選')
    if len(source.strip()) >= 200 and len(cleaned) < max(40, int(len(source.strip()) * 0.2)):
        raise RuntimeError("Qwen 回傳內容異常過短")
    return cleaned


def generate(
    api_url: str,
    model: str,
    prompt: str,
    source: str,
    timeout_seconds: int,
    context_size: int = 32768,
) -> str:
    # Qwen3 由 API think=false 關閉思考；正文仍保留足夠輸出額度。
    num_predict = max(2048, min(8192, int(len(source) * 1.5) + 2048))
    body = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
            **({'think': False} if model.split(':')[0] == 'qwen3' else {}),
            "options": {"temperature": 0, "num_ctx": context_size, "num_predict": num_predict},
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        api_url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Ollama API 失敗：{exc}") from exc
    return validate_response(payload, source)


def clean_text(
    text: str,
    api_url: str,
    model: str,
    terms: list[str],
    max_chars: int,
    timeout_seconds: int,
    context_size: int = 32768,
    cache_dir: Path | None = None,
    progress=None,
    model_digest: str | None = None,
) -> str:
    chunks = split_chunks(text, max_chars=max_chars)
    if not chunks:
        raise RuntimeError("逐字稿是空白，不能執行 Qwen 校稿")
    cleaned: list[str] = []
    def clean_chunk(chunk: str) -> str:
        prompt = make_prompt(chunk, terms)
        cache = None
        if cache_dir is not None:
            identity = json.dumps([model, model_digest or api_url, context_size, prompt, 'think-false-v1'], ensure_ascii=False)
            cache = cache_dir / (hashlib.sha256(identity.encode('utf-8')).hexdigest() + '.txt')
            if cache.exists():
                return validate_response({'response': cache.read_text(encoding='utf-8')}, chunk)
        try:
            result = generate(api_url, model, prompt, chunk, timeout_seconds, context_size)
        except OutputTruncatedError:
            if len(chunk) <= 120:
                raise
            print('單段輸出過長，自動切小後重試；已完成段落保留。', flush=True)
            result = '\n'.join(clean_chunk(part) for part in split_chunks(chunk, len(chunk) // 2))
        if cache is not None:
            atomic_write_text(cache, result)
        return result
    for index, chunk in enumerate(chunks, start=1):
        print(f"Qwen 校稿進度：{index}/{len(chunks)}", flush=True)
        if progress:
            progress(index - 1, len(chunks))
        cleaned.append(clean_chunk(chunk))
        if progress:
            progress(index, len(chunks))
    return convert_s2tw("\n".join(cleaned).strip() + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="使用本機 Ollama/Qwen 校稿逐字稿")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--model", default="qwen3:8b")
    parser.add_argument("--api-url", default="http://127.0.0.1:11434/api/generate")
    parser.add_argument("--terms", type=Path)
    parser.add_argument("--max-chars", type=int, default=900)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--context-size", type=int, default=8192)
    parser.add_argument('--cache-dir', type=Path)
    args = parser.parse_args()

    source_text = args.source.read_text(encoding="utf-8-sig")
    result = clean_text(
        source_text,
        args.api_url,
        args.model,
        load_terms(args.terms),
        args.max_chars,
        args.timeout_seconds,
        args.context_size,
        args.cache_dir,
    )
    atomic_write_text(args.destination, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
