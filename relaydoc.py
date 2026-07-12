#!/usr/bin/env python3
"""
relay-doctor — one-command health check for any OpenAI-compatible AI API / relay.

The quick smoke test you run BEFORE trusting a relay (for the deep "am I being
model-downgraded?" check, use LLMprobe). No dependencies — Python 3 stdlib only.

It checks, against your endpoint:
  1. /models          — is the endpoint reachable? how many models are exposed?
  2. chat completion  — does a real call succeed? round-trip latency, token usage
  3. streaming        — does streaming work? time-to-first-token (TTFT)

Your key is used only to make these calls; it is never stored or logged.

Usage:
    python relaydoc.py --base-url https://<relay>/v1 --api-key <key>
    python relaydoc.py --base-url https://<relay>/v1 --api-key <key> --model gpt-4o
    # reads OPENAI_BASE_URL / OPENAI_API_KEY if flags omitted
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request


def _req(url, key, payload=None, stream=False, timeout=60):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"Bearer {key}")
    if data:
        req.add_header("Content-Type", "application/json")
    return urllib.request.urlopen(req, timeout=timeout)


def check_models(base, key):
    url = base.rstrip("/") + "/models"
    try:
        t = time.time()
        resp = _req(url, key, timeout=30)
        body = json.loads(resp.read().decode())
        dt = time.time() - t
        models = [m.get("id") for m in body.get("data", [])] if isinstance(body, dict) else []
        return {"ok": True, "count": len(models), "models": models, "latency": dt}
    except urllib.error.HTTPError as e:
        return {"ok": False, "err": f"HTTP {e.code} {e.reason}"}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}


def check_chat(base, key, model):
    url = base.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": [{"role": "user", "content": "Reply with the single word: pong"}],
               "max_tokens": 8, "temperature": 0}
    try:
        t = time.time()
        resp = _req(url, key, payload, timeout=60)
        body = json.loads(resp.read().decode())
        dt = time.time() - t
        text = body.get("choices", [{}])[0].get("message", {}).get("content", "")
        usage = body.get("usage", {})
        reported_model = body.get("model", "")
        return {"ok": True, "latency": dt, "text": text.strip()[:40],
                "usage": usage, "reported_model": reported_model}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode()[:200]
        except Exception:
            pass
        return {"ok": False, "err": f"HTTP {e.code} {e.reason}", "detail": detail}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}


def check_stream(base, key, model):
    url = base.rstrip("/") + "/chat/completions"
    payload = {"model": model, "messages": [{"role": "user", "content": "Count: 1 2 3 4 5"}],
               "max_tokens": 20, "temperature": 0, "stream": True}
    try:
        t = time.time()
        resp = _req(url, key, payload, stream=True, timeout=60)
        ttft = None
        chunks = 0
        for raw in resp:
            line = raw.decode(errors="ignore").strip()
            if line.startswith("data:") and line != "data: [DONE]":
                if ttft is None:
                    ttft = time.time() - t
                chunks += 1
        return {"ok": chunks > 0, "ttft": ttft, "chunks": chunks}
    except urllib.error.HTTPError as e:
        return {"ok": False, "err": f"HTTP {e.code} {e.reason}"}
    except Exception as e:
        return {"ok": False, "err": f"{type(e).__name__}: {e}"}


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL"),
                   help="OpenAI-compatible base URL (usually ends in /v1)")
    p.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    p.add_argument("--model", help="model id to test (default: first from /models)")
    args = p.parse_args()

    if not args.base_url or not args.api_key:
        print("need --base-url and --api-key (or OPENAI_BASE_URL / OPENAI_API_KEY)", file=sys.stderr)
        return 2

    print(f"relay-doctor → {args.base_url}\n")

    healthy = True

    m = check_models(args.base_url, args.api_key)
    if m["ok"]:
        print(f"[✓] /models         reachable · {m['count']} models · {m['latency']*1000:.0f} ms")
    else:
        print(f"[✗] /models         {m['err']}  (some relays don't expose /models — not fatal)")

    model = args.model
    if not model:
        model = (m.get("models") or [None])[0]
    if not model:
        print("\n!! no model to test. Pass --model <id>.", file=sys.stderr)
        return 2
    print(f"    testing model: {model}\n")

    c = check_chat(args.base_url, args.api_key, model)
    if c["ok"]:
        u = c["usage"]
        tok = f"{u.get('total_tokens','?')} tok" if u else "no usage field"
        rm = c.get("reported_model", "")
        rm_note = "" if rm in ("", model) else f"  (reports model='{rm}')"
        print(f"[✓] chat completion works · {c['latency']*1000:.0f} ms · {tok} · reply={c['text']!r}{rm_note}")
    else:
        healthy = False
        print(f"[✗] chat completion FAILED · {c['err']}")
        if c.get("detail"):
            print(f"    {c['detail']}")

    s = check_stream(args.base_url, args.api_key, model)
    if s["ok"]:
        ttft = f"{s['ttft']*1000:.0f} ms TTFT" if s.get("ttft") else "TTFT n/a"
        print(f"[✓] streaming works · {ttft} · {s['chunks']} chunks")
    else:
        print(f"[!] streaming FAILED · {s.get('err','no chunks')}  "
              f"(some tools can fall back to non-streaming)")

    print()
    if healthy:
        print("Verdict: relay is alive and serving. Next: verify it's not downgrading "
              "the model → LLMprobe (github.com/cocodot2026/cocodot-llmprobe).")
        return 0
    print("Verdict: relay is NOT serving completions. Check key, base URL, and model id.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
