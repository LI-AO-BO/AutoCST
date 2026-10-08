"""Read-only, physical-page retrieval for a locally supplied CST PDF manual.

The original PDF is never modified. Extracted text stays in a local JSONL cache;
search results carry the source SHA-256 and 1-based PDF physical page number so
an assistant can check the original page before relying on an API description.
Install ``pypdf`` only to build a new index; reading a valid cache uses stdlib.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import unicodedata
from typing import Any, Iterator


INDEX_VERSION = 1
EVIDENCE_NOTICE = "Extracted text may contain OCR errors. Verify original physical pages before using API signatures; no match does not prove absence."


class ManualError(RuntimeError):
    """The source document or its local index could not be read safely."""


_ALIASES = {
    "自动化": ("automation", "scripting", "python", "vba"),
    "脚本": ("scripting", "script", "python", "vba"),
    "接口": ("interface", "api"),
    "建模": ("modeling", "modelling", "geometry", "history"),
    "求解": ("solver", "simulation"),
    "结果": ("results", "resulttree", "export"),
    "参数": ("parameter", "parameters"),
    "宏": ("macro", "vba"),
    "端口": ("port", "ports"),
    "材料": ("material", "materials"),
    "边界": ("boundary", "boundaries"),
    "网格": ("mesh", "meshing"),
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source(pdf: str | os.PathLike[str]) -> tuple[Path, str]:
    source = Path(pdf).expanduser().resolve()
    if not source.is_file():
        raise ManualError(f"PDF source does not exist: {source}")
    return source, _sha256(source)


def _paths(cache_dir: str | os.PathLike[str], source_hash: str) -> tuple[Path, Path]:
    root = Path(cache_dir).expanduser().resolve() / source_hash
    return root / "pages.jsonl", root / "metadata.json"


def _extract_pages(source: Path) -> Iterator[str]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ManualError("Indexing requires pypdf: install it in the Python environment used by AutoCST.") from exc
    try:
        with source.open("rb") as stream:
            reader = PdfReader(stream)
            if reader.is_encrypted and not reader.decrypt(""):
                raise ManualError("The PDF is encrypted and cannot be read without a password.")
            for page in reader.pages:
                yield page.extract_text() or ""
    except ManualError:
        raise
    except Exception as exc:
        raise ManualError(f"PDF text extraction failed: {exc}") from exc


def _load_valid_index(index: Path, metadata_path: Path, source_hash: str) -> tuple[dict[str, Any], list[dict[str, Any]]] | None:
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        if metadata.get("index_version") != INDEX_VERSION or metadata.get("source_sha256") != source_hash:
            return None
        if metadata.get("index_sha256") != _sha256(index):
            return None
        records = []
        with index.open(encoding="utf-8") as stream:
            for page, line in enumerate(stream, 1):
                record = json.loads(line)
                if record.get("page") != page or record.get("source_sha256") != source_hash or not isinstance(record.get("text"), str):
                    return None
                records.append(record)
        if len(records) != metadata.get("pages") or not records:
            return None
        return metadata, records
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def build_index(pdf: str | os.PathLike[str], cache_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Build or validate the local page index; return compact JSON metadata.

    Source replacement creates a separate hash namespace. A truncated or edited
    cache is rebuilt. Extraction errors never publish a partial completed index.
    """
    source, source_hash = _source(pdf)
    index, metadata_path = _paths(cache_dir, source_hash)
    existing = _load_valid_index(index, metadata_path, source_hash)
    if existing is not None:
        metadata, _ = existing
        return {**metadata, "pdf": str(source), "index_path": str(index), "cached": True}
    index.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    pages = empty_pages = 0
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=index.parent, suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            for pages, page_text in enumerate(_extract_pages(source), 1):
                empty_pages += not bool(page_text.strip())
                record = {"page": pages, "source_sha256": source_hash, "text": page_text}
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        if not pages:
            raise ManualError("The PDF contains no pages.")
        if _sha256(source) != source_hash:
            raise ManualError("The PDF changed during indexing; retry against the stable source.")
        metadata = {
            "index_version": INDEX_VERSION,
            "source_sha256": source_hash,
            "source_filename": source.name,
            "source_bytes": source.stat().st_size,
            "pages": pages,
            "empty_pages": empty_pages,
            "page_numbering": "1-based PDF physical pages; printed page labels may differ",
            "extraction": "pypdf text extraction; images and scanned text are not OCRed",
            "index_sha256": _sha256(temporary),
        }
        os.replace(temporary, index)
        _atomic_json(metadata_path, metadata)
        return {**metadata, "pdf": str(source), "index_path": str(index), "cached": False}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _get_pages(pdf: str | os.PathLike[str], cache_dir: str | os.PathLike[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    metadata = build_index(pdf, cache_dir)
    index, metadata_path = _paths(cache_dir, metadata["source_sha256"])
    loaded = _load_valid_index(index, metadata_path, metadata["source_sha256"])
    if loaded is None:
        raise ManualError("The manual index changed while reading; retry the operation.")
    return metadata, loaded[1]


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def _terms(query: str) -> list[str]:
    normalized = _normalize(query)
    terms = re.findall(r"[a-z0-9_]+(?:\.[a-z0-9_]+)*|[\u3400-\u9fff]+", normalized)
    for phrase, aliases in _ALIASES.items():
        if phrase in normalized:
            terms.extend(aliases)
    return list(dict.fromkeys(terms))


def _snippet(page_text: str, terms: list[str], length: int = 650) -> str:
    clean = re.sub(r"\s+", " ", page_text).strip()
    positions = []
    for term in terms:
        match = re.search(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", clean, re.IGNORECASE)
        if match:
            positions.append(match.start())
    start = max(0, min(positions, default=0) - 130)
    end = min(len(clean), start + length)
    return ("…" if start else "") + clean[start:end] + ("…" if end < len(clean) else "")


def search_manual(query: str, pdf: str | os.PathLike[str], cache_dir: str | os.PathLike[str], limit: int = 5) -> dict[str, Any]:
    """Search extracted page text; a match is evidence of text, not API validity."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("limit must be an integer between 1 and 100")
    terms = _terms(query)
    if not terms:
        raise ValueError("query must contain searchable letters, digits, or Chinese text")
    metadata, pages = _get_pages(pdf, cache_dir)
    normalized_pages = [_normalize(page["text"]) for page in pages]
    # Inverse document frequency limits the influence of ubiquitous words such
    # as 'CST', while preserving exact API member names including dotted names.
    token_patterns = {
        term: re.compile(r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])", re.IGNORECASE)
        for term in terms
    }
    counts = [{term: len(pattern.findall(text)) for term, pattern in token_patterns.items()} for text in normalized_pages]
    frequencies = {term: sum(count[term] > 0 for count in counts) for term in terms}
    weights = {term: math.log(1 + len(pages) / (1 + frequency)) for term, frequency in frequencies.items()}
    phrase = _normalize(query)
    ranked = []
    for page, normalized, count in zip(pages, normalized_pages, counts):
        matched = [term for term in terms if count[term]]
        if not matched:
            continue
        score = sum(weights[term] * (1 + math.log(count[term])) for term in matched)
        if len(terms) > 1 and phrase in normalized:
            score += sum(weights.values())
        ranked.append({
            "page": page["page"],
            "source_sha256": metadata["source_sha256"],
            "score": round(score, 4),
            "matched_terms": matched,
            "snippet": _snippet(page["text"], matched),
        })
    ranked.sort(key=lambda item: (-item["score"], item["page"]))
    return {
        "query": query,
        "pdf": metadata["pdf"],
        "source_sha256": metadata["source_sha256"],
        "total_pages": metadata["pages"],
        "matching_pages": len(ranked),
        "page_numbering": metadata["page_numbering"],
        "evidence_notice": EVIDENCE_NOTICE,
        "results": ranked[:limit],
    }


def read_pages(pdf: str | os.PathLike[str], cache_dir: str | os.PathLike[str], start: int, end: int) -> dict[str, Any]:
    """Read an inclusive range of 1-based physical PDF pages from the cache."""
    if any(isinstance(value, bool) or not isinstance(value, int) for value in (start, end)):
        raise ValueError("start and end must be integers")
    metadata, pages = _get_pages(pdf, cache_dir)
    if not 1 <= start <= end <= metadata["pages"]:
        raise ValueError(f"page range must satisfy 1 <= start <= end <= {metadata['pages']}")
    return {
        "pdf": metadata["pdf"],
        "source_sha256": metadata["source_sha256"],
        "total_pages": metadata["pages"],
        "page_numbering": metadata["page_numbering"],
        "evidence_notice": EVIDENCE_NOTICE,
        "pages": pages[start - 1:end],
    }
