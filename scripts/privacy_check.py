#!/usr/bin/env python3
"""Lint eval data for residual PII / internal ids. Stdlib only. Exit 1 if any hit."""
import json, os, re, sys
PATTERNS = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("phone", re.compile(r"(?<!\d)(?:\+?\d[\d\- ]{8,}\d)")),
    ("feishu_chat_id", re.compile(r"\boc_[a-f0-9]{10,}\b")),
    ("http_url", re.compile(r"https?://\S+")),
    ("box_path", re.compile(r"/(?:home/box|workspace)/\S+")),
    ("tokenish", re.compile(r"\b(?:sk-|Bearer |xox[baprs]-)[A-Za-z0-9_\-]{8,}")),
    ("non_placeholder_handle", re.compile(r"@(?!\{?(?:pet|user_[a-z]|_?user_[a-z])\}?\b)[A-Za-z][\w.\-]{1,30}")),
]
SKIP_KEYS = {"bot_reply", "zijun_approved_rewrite"}  # bot text kept verbatim; already scrubbed of names

def walk(obj, path="$"):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in SKIP_KEYS:
                continue
            yield from walk(v, f"{path}.{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj

def check_file(path):
    hits = []
    with open(path, encoding="utf-8") as fh:
        if path.endswith(".jsonl"):
            rows = [json.loads(l) for l in fh if l.strip()]
        else:
            rows = [json.load(fh)]
    for i, row in enumerate(rows):
        for pth, text in walk(row):
            for name, pat in PATTERNS:
                for m in pat.finditer(text):
                    if name == "email" and m.group(0).endswith((".md", ".jsonl", ".py")):
                        continue
                    hits.append((path, i, row.get("id", "?"), name, m.group(0)[:80], pth))
    return hits

def main():
    paths = sys.argv[1:] or [os.path.join(os.path.dirname(__file__), "..", "data", "golden_set.jsonl"),
                             os.path.join(os.path.dirname(__file__), "..", "data", "holdout.jsonl")]
    all_hits = []
    for p in paths:
        all_hits += check_file(p)
    for h in all_hits:
        print(f"{h[0]}#{h[1]} id={h[2]} [{h[3]}] {h[4]!r} @ {h[5]}")
    print(f"hits={len(all_hits)}")
    sys.exit(1 if all_hits else 0)

if __name__ == "__main__":
    main()
