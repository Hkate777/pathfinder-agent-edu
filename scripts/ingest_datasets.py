"""Dataset ingestion: public-licensed math datasets -> project case format.

Implements the adoption decisions from ADOPTION_NOTES.md §2.1 and the
pre-ingest validation from HK-plan v1.1 §4.3:
  * license archived next to the data (non-negotiable)
  * these datasets carry NO misconception labels — knowledge_point stays null
    until teacher annotation (they feed practice/eval layers, not the rule KB)
  * default is dry-run; nothing is written without --write

Usage:
    python scripts/ingest_datasets.py --dataset gsm8k --from-file gsm8k.jsonl
    python scripts/ingest_datasets.py --dataset gsm8k --from-file gsm8k.jsonl --write --limit 200
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "knowledge_base" / "imports"

# Registry: only A-tier (commercially usable) datasets per HK-plan §0/§4.
DATASETS = {
    "gsm8k": {
        "title": "GSM8K (OpenAI)",
        "id_prefix": "gsm8k-",
        "license": "MIT",
        "license_url": "https://github.com/openai/grade-school-math/blob/master/LICENSE",
        "source_url": "https://github.com/openai/grade-school-math",
        "format": "gsm8k_jsonl",  # {"question": str, "answer": "... #### 42"}
        "language": "en",
        "grade_band": "primary/middle",
    },
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def split_gsm8k_answer(answer: str) -> tuple[list[str], str | None]:
    """GSM8K answers end with '#### <final>'. Reasoning lines become steps."""
    lines = [line.strip() for line in answer.splitlines() if line.strip()]
    final = None
    steps: list[str] = []
    for line in lines:
        match = re.match(r"^####\s*(.+)$", line)
        if match:
            final = match.group(1).strip()
        else:
            steps.append(line)
    return steps, final


def convert_record(idx: int, raw: dict, meta: dict) -> dict | None:
    if meta["format"] == "gsm8k_jsonl":
        question = (raw.get("question") or "").strip()
        steps, final = split_gsm8k_answer(raw.get("answer") or "")
        if not question or not steps:
            return None
        return {
            "id": f"{meta['id_prefix']}{idx:06d}",
            "question": question,
            "steps": steps,
            "answer_final": final,
            # NO misconception label: these are practice/eval items, not rule KB.
            "knowledge_point": None,
            "source": {
                "dataset": meta["title"],
                "license": meta["license"],
                "source_url": meta["source_url"],
                "language": meta["language"],
                "grade_band": meta["grade_band"],
                "ingested_at": utc_now(),
            },
        }
    return None


def run(dataset: str, from_file: Path | None, download: bool, limit: int, write: bool) -> int:
    meta = DATASETS.get(dataset)
    if meta is None:
        print(f"[ingest] unknown dataset '{dataset}'. Available: {', '.join(DATASETS)}")
        return 2
    if download:
        print("[ingest] --download is intentionally not implemented: fetch the raw "
              "JSONL from the dataset's official source yourself, then pass --from-file. "
              "This keeps the pipeline offline-first and the license chain manual.")
        return 2
    if from_file is None or not Path(from_file).exists():
        print("[ingest] --from-file is required (a local JSONL export of the dataset).")
        return 2

    records: list[dict] = []
    skipped = 0
    with open(from_file, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                skipped += 1
                continue
            converted = convert_record(len(records), raw, meta)
            if converted is None:
                skipped += 1
                continue
            records.append(converted)
            if limit and len(records) >= limit:
                break

    if not records:
        print(f"[ingest] 0 usable records in {from_file} (skipped {skipped}).")
        return 1

    target = OUT_DIR / dataset
    summary = (
        f"[ingest] {meta['title']}  license={meta['license']}\n"
        f"  usable: {len(records)}  skipped: {skipped}  limit: {limit or 'none'}\n"
        f"  would write:\n"
        f"    {target / 'items.jsonl'}   ({len(records)} items, knowledge_point=null until annotation)\n"
        f"    {target / 'LICENSE.md'}    (license archived per HK-plan §4.3)\n"
        f"  NOTE: no misconception labels — do not feed the rule KB without teacher annotation."
    )
    print(summary)
    if not write:
        print("[ingest] DRY-RUN: nothing written. Add --write to persist.")
        return 0

    target.mkdir(parents=True, exist_ok=True)
    with open(target / "items.jsonl", "w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    (target / "LICENSE.md").write_text(
        f"# {meta['title']}\n\n"
        f"- License: **{meta['license']}** — {meta['license_url']}\n"
        f"- Source: {meta['source_url']}\n"
        f"- Ingested: {utc_now()}\n"
        f"- Items: {len(records)} (from {from_file})\n\n"
        f"Per HK-plan §0/§4.3: license archived at ingest time; commercial use allowed "
        f"under the license; attribution obligations must be preserved downstream.\n",
        encoding="utf-8",
    )
    print(f"[ingest] WROTE {len(records)} items -> {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dataset", required=True, choices=sorted(DATASETS))
    ap.add_argument("--from-file", type=Path, help="local JSONL export of the dataset")
    ap.add_argument("--download", action="store_true",
                    help="(not implemented) downloading stays manual so the license chain is auditable")
    ap.add_argument("--limit", type=int, default=0, help="cap records (0 = all)")
    ap.add_argument("--write", action="store_true", help="persist output (default dry-run)")
    args = ap.parse_args(argv)
    return run(args.dataset, args.from_file, args.download, args.limit, args.write)


if __name__ == "__main__":
    sys.exit(main())
