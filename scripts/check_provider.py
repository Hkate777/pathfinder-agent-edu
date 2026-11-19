"""Provider connectivity check: which Pathfinder slots can this endpoint fill?

Run BEFORE spending real evaluation calls. Reads .env / environment via
llm.Config, then probes (cheapest-first):
  1. GET  /models          — what the endpoint exposes
  2. POST /chat/completions — text slot (tiny prompt, few tokens)
  3. POST /embeddings       — embedding slot (grounding needs this in online mode)

Output: a slot table + a ready-to-paste .env block + the exact next command.

Usage:
    python scripts/check_provider.py            # uses .env / environment
    python scripts/check_provider.py --vision-image samples/photo.jpg   # also probe vision
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from llm import Config  # noqa: E402


def _headers(cfg: Config) -> dict:
    return {
        "Authorization": f"Bearer {cfg.api_key}",
        "Content-Type": "application/json",
        **dict(cfg.extra_headers or {}),
    }


def probe_models(cfg: Config) -> tuple[str, list[str]]:
    url = cfg.base_url.rstrip("/") + "/models"
    try:
        resp = requests.get(url, headers=_headers(cfg), timeout=cfg.timeout)
        if resp.status_code != 200:
            return f"HTTP {resp.status_code}", []
        data = resp.json()
        ids = sorted(m.get("id", "") for m in data.get("data", []))
        return "ok", ids
    except Exception as exc:  # noqa: BLE001
        return f"unreachable ({type(exc).__name__}: {str(exc)[:80]})", []


def _extract_reply(data: dict) -> str:
    """Some models return content=None and put tokens in reasoning_content."""
    choices = data.get("choices") or []
    message = (choices[0].get("message") if choices else None) or {}
    return (message.get("content") or message.get("reasoning_content")
            or message.get("reasoning") or "")


def probe_chat(cfg: Config, model: str) -> str:
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
        "max_tokens": 32,
        "temperature": 0,
    }
    try:
        resp = requests.post(url, headers=_headers(cfg), json=body, timeout=cfg.timeout)
        if resp.status_code != 200:
            return f"HTTP {resp.status_code}: {resp.text[:100]}"
        return f"ok (reply={_extract_reply(resp.json())[:20]!r})"
    except Exception as exc:  # noqa: BLE001
        return f"failed ({type(exc).__name__}: {str(exc)[:80]})"


def probe_embeddings(cfg: Config, model: str) -> str:
    url = cfg.base_url.rstrip("/") + "/embeddings"
    body = {"model": model, "input": "grounding probe"}
    try:
        resp = requests.post(url, headers=_headers(cfg), json=body, timeout=cfg.timeout)
        if resp.status_code != 200:
            return f"HTTP {resp.status_code}: {resp.text[:100]}"
        dims = len(resp.json()["data"][0]["embedding"])
        return f"ok (dim={dims})"
    except Exception as exc:  # noqa: BLE001
        return f"failed ({type(exc).__name__}: {str(exc)[:80]})"


def probe_vision(cfg: Config, model: str, image_path: Path) -> str:
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    encoded = base64.b64encode(image_path.read_bytes()).decode()
    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Transcribe any text in this image. Reply with the text only."},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
            ],
        }],
        "max_tokens": 64,
        "temperature": 0,
    }
    try:
        resp = requests.post(url, headers=_headers(cfg), json=body, timeout=cfg.timeout)
        if resp.status_code != 200:
            return f"HTTP {resp.status_code}: {resp.text[:100]}"
        return f"ok (reply={_extract_reply(resp.json())[:40]!r})"
    except Exception as exc:  # noqa: BLE001
        return f"failed ({type(exc).__name__}: {str(exc)[:80]})"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--vision-image", type=Path, help="small PNG/JPG to probe the vision slot")
    args = ap.parse_args(argv)

    cfg = Config.from_env()
    if cfg.offline:
        print("[provider] PATHFINDER_OFFLINE=1 — set it to 0 in .env to probe the cloud route.")
        return 2
    if not cfg.api_key or not cfg.base_url:
        print("[provider] LLM_API_KEY / LLM_BASE_URL are empty.\n"
              "[provider] Fill .env first (see .env.example), then re-run. Nothing probed.")
        return 2

    print(f"[provider] endpoint = {cfg.base_url}")
    status, model_ids = probe_models(cfg)
    print(f"[provider] /models            : {status}"
          + (f" ({len(model_ids)} models)" if model_ids else ""))

    results = {}

    text_model = cfg.text_model
    if text_model:
        results["TEXT"] = (text_model, probe_chat(cfg, text_model))
    else:
        results["TEXT"] = ("<unset>", "not configured")

    emb_model = cfg.embedding_model
    if cfg.embedding_mode == "local":
        results["EMBEDDING"] = ("local-hash", "ok — local deterministic, no API call (hybrid run)")
        local_embedding = True
    elif emb_model:
        results["EMBEDDING"] = (emb_model, probe_embeddings(cfg, emb_model))
        local_embedding = False
    else:
        results["EMBEDDING"] = ("<unset>", "not configured — set EMBEDDING_MODE=local or an embedding model")
        local_embedding = False

    if args.vision_image and cfg.vision_model:
        results["VISION"] = (cfg.vision_model, probe_vision(cfg, cfg.vision_model, args.vision_image))
    elif cfg.vision_model:
        results["VISION"] = (cfg.vision_model, "configured; pass --vision-image <file> to probe for real")
    else:
        results["VISION"] = ("<unset>", "not configured — photo route disabled")

    print("\nSlot          Model                          Status")
    for slot, (model, status_line) in results.items():
        print(f"{slot:<14}{model:<31}{status_line}")

    blockers = [s for s, (_, line) in results.items()
                if s != "VISION" and not line.startswith("ok")]
    if model_ids:
        hints = [m for m in model_ids
                 if any(k in m.lower() for k in ("embed", "bge", "text-embedding"))]
        if blockers and hints:
            print(f"\n[provider] embedding-capable models on this endpoint: {hints[:5]}")

    print("\nNext steps:")
    if blockers:
        print(f"  - fix failing required slots: {', '.join(blockers)} (online mode needs TEXT + EMBEDDING)")
    else:
        print("  - all required slots OK:  python eval.py --set samples --online --save")
        print("  - then the challenge set:  python eval.py --set challenge --online")
    if "VISION" in blockers or results.get("VISION", ("", ""))[1].startswith("HTTP"):
        print("  - vision failing: photo route stays disabled; text/manual input still works")
    return 0 if not blockers else 1


if __name__ == "__main__":
    sys.exit(main())
