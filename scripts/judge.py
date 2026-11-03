#!/usr/bin/env python3
"""
Streak Pet Bot dialogue judge (Zijun's standard) — portable CLI, Python 3.8+, stdlib only.

Calls any OpenAI-compatible chat endpoint (or an Anthropic Messages endpoint) configured by env:
  JUDGE_API_BASE   e.g. https://api.openai.com/v1   (".../v1" -> POST .../chat/completions)
  JUDGE_API_KEY    API key (read from env only, never written anywhere)
  JUDGE_MODEL      e.g. gpt-4.1 / deepseek-chat / claude-sonnet-...
  JUDGE_API_STYLE  openai (default) | anthropic   (anthropic -> POST {base}/v1/messages)
  optional: JUDGE_TEMPERATURE (unset = provider default) JUDGE_MAX_TOKENS (2048) JUDGE_TIMEOUT (120) JUDGE_STREAM (0|1) JUDGE_RETRIES (2)

Modes:
  single     --persona ... --context ... --query ... --reply ...   (Markdown, or --json)
  rewrite    same, plus --rewrite-only (or omit --reply)
  case       --case-id Q08_B01                                      (from data/golden_set.jsonl or data/holdout.jsonl)
  batch      --input cases.jsonl|.csv|.tsv|.json --output judged.jsonl|.csv|.tsv
  benchmark  --benchmark [--limit N] [--ids Q08_B01,Q14_B06] [--report out.md]
  compare    --compare --input ab.jsonl --output ab_judged.jsonl [--col-a reply_a --col-b reply_b]
  --dry-run  prints the prompts that would be sent; no network call, no key needed.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JUDGE_PROMPT_PATH = os.path.join(SKILL_DIR, "prompts", "judge_system_prompt.md")
REWRITE_PROMPT_PATH = os.path.join(SKILL_DIR, "prompts", "rewrite_prompt.md")
GOLDEN_PATH = os.path.join(SKILL_DIR, "data", "golden_set.jsonl")
HOLDOUT_PATH = os.path.join(SKILL_DIR, "data", "holdout.jsonl")

DIMS = ["A", "B-1", "B-2", "C-1", "C-2"]
GOOD, OK, BAD, REJECT, UNSURE = "好", "还可以", "不太好", "hard_reject", "无法判断"
TIE_EPS = 0.15
csv.field_size_limit(10 ** 8)


# ----------------------------------------------------------------------------- LLM client
class LLMError(RuntimeError):
    pass


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _endpoint(base: str, style: str) -> str:
    base = base.rstrip("/")
    has_version = re.search(r"/v\d+[a-z]*$", base) is not None
    if style == "anthropic":
        return base + ("/messages" if has_version else "/v1/messages")
    if base.endswith("/chat/completions"):
        return base
    return base + ("/chat/completions" if has_version else "/v1/chat/completions")


def _read_sse(resp, style: str) -> str:
    out: List[str] = []
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            ev = json.loads(data)
        except json.JSONDecodeError:
            continue
        if style == "anthropic":
            if ev.get("type") == "content_block_delta":
                out.append((ev.get("delta") or {}).get("text", ""))
        else:
            for ch in ev.get("choices") or []:
                out.append(((ch.get("delta") or {}).get("content")) or "")
    return "".join(out)


def call_llm(system: str, user: str) -> Dict[str, str]:
    base, key, model = _env("JUDGE_API_BASE"), _env("JUDGE_API_KEY"), _env("JUDGE_MODEL")
    missing = [n for n, v in (("JUDGE_API_BASE", base), ("JUDGE_API_KEY", key), ("JUDGE_MODEL", model)) if not v]
    if missing:
        raise LLMError("missing env: " + ", ".join(missing) + " (or use --dry-run)")
    style = (_env("JUDGE_API_STYLE", "openai") or "openai").lower()
    stream = _env("JUDGE_STREAM", "0") in ("1", "true", "yes")
    temperature = _env("JUDGE_TEMPERATURE")
    max_tokens = int(_env("JUDGE_MAX_TOKENS", "2048"))
    timeout = float(_env("JUDGE_TIMEOUT", "120"))
    retries = int(_env("JUDGE_RETRIES", "2"))
    url = _endpoint(base, style)
    headers = {"Content-Type": "application/json", "User-Agent": "streak-pet-bot-eval-agent/1.0",
               "Accept": "text/event-stream" if stream else "application/json"}
    if style == "anthropic":
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
        body = {"model": model, "max_tokens": max_tokens, "stream": stream,
                "system": system, "messages": [{"role": "user", "content": user}]}
    else:
        headers["Authorization"] = "Bearer " + key
        body = {"model": model, "max_tokens": max_tokens, "stream": stream,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if temperature is not None:
        body["temperature"] = float(temperature)
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if stream or "event-stream" in (resp.headers.get("Content-Type") or ""):
                    text = _read_sse(resp, style)
                else:
                    obj = json.loads(resp.read().decode("utf-8"))
                    if style == "anthropic":
                        text = "".join(b.get("text", "") for b in obj.get("content", []) if b.get("type") == "text")
                    else:
                        text = (obj["choices"][0]["message"].get("content") or "")
            if not text.strip():
                raise LLMError("empty response")
            return {"text": text, "model": model}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:300]
            last = LLMError(f"HTTP {e.code}: {detail}")
            if e.code == 400 and "temperature" in detail and "temperature" in body:
                body.pop("temperature")  # some models only accept the default temperature
                continue
            if e.code in (400, 401, 403, 404):
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError, LLMError, KeyError, ValueError) as e:
            last = LLMError(str(e))
        time.sleep(2 * (attempt + 1))
    raise last or LLMError("unknown error")


# ----------------------------------------------------------------------------- case normalisation
def _maybe_json(v: Any) -> Any:
    if isinstance(v, str) and v.strip()[:1] in ("[", "{"):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return v
    return v


def format_turns(prior: Any) -> str:
    prior = _maybe_json(prior)
    if not prior:
        return "（无前序上下文）"
    if isinstance(prior, list):
        lines = []
        for t in prior:
            if isinstance(t, dict):
                spk = t.get("speaker", "user")
                who = "🐾 pet" if spk in ("streak_pet", "pet", "bot") else f"👤 {spk}"
                txt, zh = t.get("text", ""), t.get("zh", "")
                lines.append(f"{who}: {txt}" + (f" （{zh}）" if zh and zh != txt else ""))
            else:
                lines.append(str(t))
        return "\n".join(lines)
    return str(prior)


def _text_of(v: Any) -> str:
    v = _maybe_json(v)
    if isinstance(v, dict):
        txt, zh = v.get("text", ""), v.get("zh", "")
        spk = v.get("speaker")
        s = (f"({spk}) " if spk else "") + txt
        return s + (f" （{zh}）" if zh and zh != txt else "")
    return "" if v is None else str(v)


def extract_case_fields(rec: Dict[str, Any], reply_key: Optional[str] = None) -> Dict[str, Any]:
    persona = _maybe_json(rec.get("persona"))
    if isinstance(persona, dict):
        style = persona.get("soul_style") or persona.get("style") or ""
        mbti = persona.get("mbti") or rec.get("mbti") or ""
        bot_name = persona.get("bot_name") or rec.get("bot_name") or "{pet}"
    else:
        style = str(persona or rec.get("soul_style") or "")
        mbti = str(rec.get("mbti") or "")
        bot_name = str(rec.get("bot_name") or "{pet}")
    if reply_key:
        reply = rec.get(reply_key, "")
    elif "bot_reply" in rec:
        reply = rec.get("bot_reply")
    else:
        reply = rec.get("reply", rec.get("case_text", ""))
    return {
        "id": str(rec.get("id") or rec.get("case_id") or ""),
        "category": rec.get("category") or rec.get("case_type") or "",
        "persona": style or "未标注",
        "mbti": mbti,
        "bot_name": bot_name,
        "lang": rec.get("lang") or "",
        "context": format_turns(rec.get("prior_turns") or rec.get("context") or rec.get("dialogue_history")),
        "query": _text_of(rec.get("query") or rec.get("user_query")),
        "reply": _text_of(reply).strip(),
    }


JSON_SPEC = (
    "\n### 输出格式\n只输出一个合法 JSON 对象，不要任何其他文字：\n"
    "{\n"
    '  "gate": "pass" | "fail",\n'
    '  "verdict": "好" | "还可以" | "不太好" | "hard_reject" | "无法判断",\n'
    '  "scores": {"A": 0-3, "B-1": 0-3, "B-2": 0-3, "C-1": 0-3, "C-2": 0-3},\n'
    '  "weighted_score": number,\n'
    '  "why": "Zijun 口吻一句话，20 字左右",\n'
    '  "missing_context": "无法判断时写缺什么，否则空字符串",\n'
    '  "rewrite_text": "1-2 行最佳回复（与用户同语种，多行用 / 分隔）",\n'
    '  "rewrite_zh": "中文翻译"\n'
    "}"
)
MD_SPEC = (
    "\n### 输出格式\n按 Zijun 口吻输出：\n"
    "1. 底线闸门：通过 / 不通过（原因）\n"
    "2. 结论：好 (2.5–3分) / 还可以 (2分) / 不太好 (0–1.5分) / hard_reject / 无法判断（缺什么）\n"
    "3. 五维打分：A x | B-1 x | B-2 x | C-1 x | C-2 x | 加权总分 x.xx\n"
    "4. 一句话原因\n"
    "5. 改写：1–2 行母语短句 + 中文翻译"
)


def build_user_prompt(f: Dict[str, Any], rewrite_only: bool = False, as_json: bool = False) -> str:
    parts = [
        "请按 Zijun 的 Streak Pet Bot 对话效果标准处理以下对话：",
        f"- Case ID: {f.get('id') or 'N/A'}",
        f"- 场景分类: {f.get('category') or '未标注'}",
        f"- 宠物人设: {f.get('persona')} | MBTI: {f.get('mbti') or 'N/A'} | 宠物名: {f.get('bot_name') or '{pet}'}",
    ]
    if f.get("lang"):
        parts.append(f"- 语言: {f['lang']}")
    parts.append("- 说明: {user_a}/{user_b}/{pet} 是脱敏占位符，属于合法 @ 提及。")
    parts.append(f"\n### 前序对话上下文\n{f.get('context') or '（无）'}")
    parts.append(f"\n### 用户当前 @ / query\n{f.get('query') or '（无，可能是主动发言或只有上下文）'}")
    if rewrite_only or not f.get("reply"):
        if f.get("reply"):
            parts.append(f"\n### 线上原回复（参考，不打分）\n{f['reply']}")
        parts.append("\n### 任务\n写出 Zijun 会判“好”的宠物回复。")
        if as_json:
            parts.append('只输出 JSON：{"rewrite_text": "...", "rewrite_zh": "...", "why": "..."}')
    else:
        parts.append(f"\n### 待评测的宠物回复\n{f['reply']}")
        parts.append(JSON_SPEC if as_json else MD_SPEC)
    return "\n".join(parts)


# ----------------------------------------------------------------------------- scoring rules
def _num(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        if isinstance(v, str):
            m = re.search(r"-?\d+(\.\d+)?", v)
            return float(m.group(0)) if m else None
        return float(v)
    except (TypeError, ValueError):
        return None


def norm_scores(raw: Any) -> Dict[str, Optional[float]]:
    raw = _maybe_json(raw)
    if not isinstance(raw, dict):
        return {d: None for d in DIMS}
    lut = {re.sub(r"[\s_\-]", "", str(k)).upper(): v for k, v in raw.items()}
    return {d: _num(lut.get(d.replace("-", "").upper())) for d in DIMS}


def weighted(s: Dict[str, Optional[float]]) -> Optional[float]:
    if any(s.get(d) is None for d in DIMS):
        return None
    return round(s["A"] * 0.25 + (s["B-1"] + s["B-2"]) / 2 * 0.35 + (s["C-1"] + s["C-2"]) / 2 * 0.40, 2)


def rule_verdict(s: Dict[str, Optional[float]]) -> Optional[str]:
    vals = [s.get(d) for d in DIMS]
    if any(v is None for v in vals):
        return None
    if min(vals) <= 1:
        return BAD
    if max(vals) >= 3 and min(vals) >= 2:
        return GOOD
    return OK


def normalize_verdict(v: Any) -> str:
    t = str(v or "").strip().lower()
    if not t:
        return ""
    if "reject" in t or "不通过" in t:
        return REJECT
    if "无法" in t or "缺上下文" in t or "unable" in t:
        return UNSURE
    if "不太好" in t or "不好" in t or t.startswith("bad") or "差" in t:
        return BAD
    if "还可以" in t or "一般" in t or "可以" in t or "mixed" in t or "ok" in t:
        return OK
    if "好" in t or "good" in t:
        return GOOD
    return str(v)


def parse_json(text: str) -> Dict[str, Any]:
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    m = re.search(r"\{[\s\S]*\}", t)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {"raw_output": text}


def finalize(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """Recompute weighted score and verdict locally so the rule is applied consistently."""
    s = norm_scores(parsed.get("scores"))
    llm_v = normalize_verdict(parsed.get("verdict"))
    gate_fail = str(parsed.get("gate", "")).lower() in ("fail", "不通过")
    if gate_fail or llm_v == REJECT:
        final = REJECT
    elif llm_v == UNSURE:
        final = UNSURE
    else:
        final = rule_verdict(s) or llm_v
    return {"scores": s, "weighted_score": weighted(s), "llm_verdict": llm_v, "verdict": final}


# ----------------------------------------------------------------------------- core calls
def load_text(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def judge_fields(f: Dict[str, Any], system: str, as_json: bool = True, dry: bool = False) -> Dict[str, Any]:
    prompt = build_user_prompt(f, as_json=as_json)
    if dry:
        return {"id": f["id"], "dry_run_prompt": prompt}
    res = call_llm(system, prompt)
    if not as_json:
        return {"id": f["id"], "model": res["model"], "output": res["text"]}
    parsed = parse_json(res["text"])
    out = {"id": f["id"], "model": res["model"]}
    out.update(finalize(parsed))
    for k in ("why", "missing_context", "rewrite_text", "rewrite_zh", "raw_output"):
        if parsed.get(k):
            out[k] = parsed[k]
    return out


def rewrite_fields(f: Dict[str, Any], as_json: bool, dry: bool = False) -> Dict[str, Any]:
    system = load_text(REWRITE_PROMPT_PATH)
    prompt = build_user_prompt(f, rewrite_only=True, as_json=as_json)
    if dry:
        return {"id": f["id"], "dry_run_prompt": prompt}
    res = call_llm(system, prompt)
    if as_json:
        out = {"id": f["id"], "model": res["model"]}
        out.update(parse_json(res["text"]))
        return out
    return {"id": f["id"], "model": res["model"], "output": res["text"]}


def safe(fn, *a, **kw) -> Dict[str, Any]:
    try:
        return fn(*a, **kw)
    except LLMError as e:
        return {"error": str(e)}


# ----------------------------------------------------------------------------- IO
def read_records(path: str) -> List[Dict[str, Any]]:
    ext = os.path.splitext(path)[1].lower()
    with open(path, encoding="utf-8-sig", newline="") as fh:
        if ext == ".jsonl":
            return [json.loads(l) for l in fh if l.strip()]
        if ext == ".json":
            data = json.load(fh)
            return data if isinstance(data, list) else data.get("cases", [data])
        if ext in (".csv", ".tsv"):
            return list(csv.DictReader(fh, delimiter="\t" if ext == ".tsv" else ","))
    raise SystemExit(f"unsupported input type: {ext} (use .jsonl/.json/.csv/.tsv)")


def _flat(row: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in row.items():
        if k.endswith("scores") and isinstance(v, dict):
            for d in DIMS:
                out[f"{k}.{d}"] = v.get(d)
        elif isinstance(v, (dict, list)):
            out[k] = json.dumps(v, ensure_ascii=False)
        else:
            out[k] = v
    return out


def write_records(path: str, rows: List[Dict[str, Any]]) -> None:
    ext = os.path.splitext(path)[1].lower()
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    if ext in (".csv", ".tsv"):
        flat = [_flat(r) for r in rows]
        keys: List[str] = []
        for r in flat:
            keys += [k for k in r if k not in keys]
        with open(path, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, delimiter="\t" if ext == ".tsv" else ",")
            w.writeheader()
            w.writerows(flat)
    elif ext == ".json":
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def find_case(case_id: str) -> Dict[str, Any]:
    for p in (GOLDEN_PATH, HOLDOUT_PATH):
        if os.path.exists(p):
            for r in read_records(p):
                if case_id in (r.get("id"), r.get("q_num")) or str(r.get("id", "")).startswith(case_id + "_"):
                    return r
    raise SystemExit(f"case {case_id!r} not found in data/golden_set.jsonl or data/holdout.jsonl")


def pmap(fn, items: List[Any], workers: int) -> List[Any]:
    if workers <= 1:
        out = []
        for i, it in enumerate(items, 1):
            out.append(fn(it))
            print(f"  [{i}/{len(items)}] done", file=sys.stderr)
        return out
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(fn, items))


# ----------------------------------------------------------------------------- modes
def run_batch(args, system: str) -> None:
    recs = read_records(args.input)[: args.limit or None]

    def one(rec):
        f = extract_case_fields(rec, args.reply_col)
        res = safe(rewrite_fields, f, True, args.dry_run) if args.rewrite_only else safe(judge_fields, f, system, True, args.dry_run)
        row = dict(rec)
        row.update({("judge_" + k if k != "id" else "id"): v for k, v in res.items()})
        return row

    rows = pmap(one, recs, args.workers)
    if args.output:
        write_records(args.output, rows)
        print(f"wrote {len(rows)} rows -> {args.output}", file=sys.stderr)
    else:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
    if not args.rewrite_only and not args.dry_run:
        counts: Dict[str, int] = {}
        for r in rows:
            v = r.get("judge_verdict") or ("error" if r.get("judge_error") else "?")
            counts[v] = counts.get(v, 0) + 1
        print("verdicts: " + json.dumps(counts, ensure_ascii=False), file=sys.stderr)


def run_benchmark(args, system: str) -> None:
    recs = read_records(GOLDEN_PATH)
    if args.ids:
        want = [x.strip() for x in args.ids.split(",") if x.strip()]
        recs = [r for r in recs if any(r["id"] == w or r["id"].startswith(w + "_") or r.get("q_num") == w for w in want)]
    recs = recs[: args.limit or None]
    if args.dry_run:
        for r in recs:
            print(json.dumps(judge_fields(extract_case_fields(r), system, True, True), ensure_ascii=False))
        return

    def one(rec):
        return rec, safe(judge_fields, extract_case_fields(rec), system, True, False)

    results = pmap(one, recs, args.workers)
    rows, agree, bad_agree, n_ok, within = [], 0, 0, 0, 0
    abs_err = {d: [] for d in DIMS}
    for rec, res in results:
        gold_v = normalize_verdict(rec.get("zijun_verdict"))
        gold_s = norm_scores(rec.get("zijun_scores"))
        pred_v = res.get("verdict", "error" if res.get("error") else "")
        row = {"id": rec["id"], "zijun_verdict": gold_v, "judge_verdict": pred_v,
               "zijun_weighted": weighted(gold_s), "judge_weighted": res.get("weighted_score"),
               "zijun_scores": gold_s, "judge_scores": res.get("scores"),
               "zijun_reason": rec.get("zijun_reason"), "judge_why": res.get("why"), "error": res.get("error")}
        rows.append(row)
        if res.get("error"):
            continue
        n_ok += 1
        agree += int(pred_v == gold_v)
        bad_agree += int((pred_v in (BAD, REJECT)) == (gold_v in (BAD, REJECT)))
        ps = res.get("scores") or {}
        for d in DIMS:
            if ps.get(d) is not None and gold_s.get(d) is not None:
                abs_err[d].append(abs(ps[d] - gold_s[d]))
        if row["judge_weighted"] is not None and row["zijun_weighted"] is not None:
            within += int(abs(row["judge_weighted"] - row["zijun_weighted"]) <= 0.5)
    mae = {d: (round(sum(v) / len(v), 2) if v else None) for d, v in abs_err.items()}
    summary = {
        "model": _env("JUDGE_MODEL"), "cases": len(recs), "judged_ok": n_ok,
        "verdict_agreement_3way": f"{agree}/{n_ok}", "bad_case_detection_agreement": f"{bad_agree}/{n_ok}",
        "weighted_within_0.5": f"{within}/{n_ok}", "per_dim_MAE": mae,
    }
    print("\n| id | Zijun | judge | Zijun total | judge total | judge why |\n|---|---|---|---|---|---|")
    for r in rows:
        why = (r.get("judge_why") or r.get("error") or "").replace("|", "/").replace("\n", " ")[:60]
        print(f"| {r['id']} | {r['zijun_verdict']} | {r['judge_verdict']} | {r['zijun_weighted']} | {r['judge_weighted']} | {why} |")
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=2))
    if args.output:
        write_records(args.output, rows)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write("# Benchmark vs Zijun golden set\n\n```json\n" + json.dumps(summary, ensure_ascii=False, indent=2) + "\n```\n\n")
            fh.write("| id | Zijun | judge | Zijun total | judge total | Zijun reason | judge why |\n|---|---|---|---|---|---|---|\n")
            for r in rows:
                cells = [r["id"], r["zijun_verdict"], r["judge_verdict"], r["zijun_weighted"], r["judge_weighted"],
                         r.get("zijun_reason") or "", r.get("judge_why") or r.get("error") or ""]
                fh.write("| " + " | ".join(str(c).replace("|", "/").replace("\n", " ") for c in cells) + " |\n")


def run_compare(args, system: str) -> None:
    recs = read_records(args.input)[: args.limit or None]
    col_a, col_b = args.col_a, args.col_b

    def one(rec):
        fa, fb = extract_case_fields(rec, col_a), extract_case_fields(rec, col_b)
        if args.dry_run:
            return {"id": fa["id"], "prompt_a": build_user_prompt(fa, as_json=True), "prompt_b": build_user_prompt(fb, as_json=True)}
        ra, rb = safe(judge_fields, fa, system), safe(judge_fields, fb, system)
        wa, wb = ra.get("weighted_score"), rb.get("weighted_score")
        if wa is None or wb is None:
            res = "error"
        elif wa - wb > TIE_EPS:
            res = "A_win"
        elif wb - wa > TIE_EPS:
            res = "B_win"
        else:
            res = "tie"
        row = {"id": fa["id"], "result": res, "a_verdict": ra.get("verdict"), "b_verdict": rb.get("verdict"),
               "a_weighted": wa, "b_weighted": wb, "a_scores": ra.get("scores"), "b_scores": rb.get("scores"),
               "a_why": ra.get("why"), "b_why": rb.get("why"), "error": ra.get("error") or rb.get("error")}
        return row

    rows = pmap(one, recs, args.workers)
    if args.dry_run:
        for r in rows:
            print(json.dumps(r, ensure_ascii=False))
        return
    if args.output:
        write_records(args.output, rows)
    valid = [r for r in rows if r["result"] != "error"]
    n = len(valid) or 1
    win = sum(r["result"] == "A_win" for r in valid)
    lose = sum(r["result"] == "B_win" for r in valid)
    tie = sum(r["result"] == "tie" for r in valid)

    def mean(side, d):
        v = [r[f"{side}_scores"][d] for r in valid if r.get(f"{side}_scores") and r[f"{side}_scores"].get(d) is not None]
        return round(sum(v) / len(v), 2) if v else None

    def rate(side, label):
        return f"{sum(r[f'{side}_verdict'] == label for r in valid)}/{len(valid)}"

    summary = {
        "pairs": len(rows), "judged_ok": len(valid), "A_win": win, "tie": tie, "B_win": lose,
        "GSB_score_A_vs_B": round((win - lose) / n, 3), "tie_threshold": TIE_EPS,
        "A_good_rate": rate("a", GOOD), "B_good_rate": rate("b", GOOD),
        "A_bad_rate": rate("a", BAD), "B_bad_rate": rate("b", BAD),
        "per_dim_mean": {d: {"A": mean("a", d), "B": mean("b", d)} for d in DIMS},
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def main() -> None:
    ap = argparse.ArgumentParser(description="Streak Pet Bot dialogue judge (Zijun standard). Env: JUDGE_API_BASE, JUDGE_API_KEY, JUDGE_MODEL [, JUDGE_API_STYLE=openai|anthropic].")
    ap.add_argument("--persona", help="sharp_tongued | gentle | tsundere | cool")
    ap.add_argument("--mbti", default="")
    ap.add_argument("--bot-name", default="{pet}")
    ap.add_argument("--category", default="")
    ap.add_argument("--lang", default="")
    ap.add_argument("--context", default="", help='prior turns, e.g. "user_a: ... / pet: ..."')
    ap.add_argument("--query", default="")
    ap.add_argument("--reply", default="", help="bot reply to judge (omit to only rewrite)")
    ap.add_argument("--case-id", help="load a case from data/golden_set.jsonl or data/holdout.jsonl")
    ap.add_argument("--rewrite-only", action="store_true", help="only write the best reply (prompts/rewrite_prompt.md)")
    ap.add_argument("--json", action="store_true", help="single mode: print structured JSON instead of Markdown")
    ap.add_argument("-i", "--input", help="batch / compare input: .jsonl .json .csv .tsv")
    ap.add_argument("-o", "--output", help="output file: .jsonl .json .csv .tsv")
    ap.add_argument("--reply-col", default=None, help="batch: column holding the reply (default bot_reply/reply/case_text)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=1, help="parallel requests")
    ap.add_argument("--benchmark", action="store_true", help="judge data/golden_set.jsonl and compare with Zijun's labels")
    ap.add_argument("--ids", help="benchmark: comma-separated case ids (Q08 or Q08_B01)")
    ap.add_argument("--report", help="benchmark: write a Markdown report here")
    ap.add_argument("--compare", action="store_true", help="A/B mode: judge two reply columns per row")
    ap.add_argument("--col-a", default="reply_a")
    ap.add_argument("--col-b", default="reply_b")
    ap.add_argument("--system-prompt", default=JUDGE_PROMPT_PATH, help="override the judge system prompt file")
    ap.add_argument("--dry-run", action="store_true", help="print prompts, do not call the API")
    args = ap.parse_args()

    system = load_text(args.system_prompt)
    if args.benchmark:
        return run_benchmark(args, system)
    if args.compare:
        if not args.input:
            ap.error("--compare needs --input")
        return run_compare(args, system)
    if args.input:
        return run_batch(args, system)

    if args.case_id:
        f = extract_case_fields(find_case(args.case_id))
    else:
        if not (args.query or args.context or args.reply):
            ap.error("give --case-id, --input, --benchmark, or --query/--context/--reply")
        f = {"id": "cli", "category": args.category, "persona": args.persona or "未标注", "mbti": args.mbti,
             "bot_name": args.bot_name, "lang": args.lang, "context": args.context or "（无）",
             "query": args.query, "reply": args.reply.strip()}
    try:
        if args.rewrite_only or not f["reply"]:
            res = rewrite_fields(f, args.json, args.dry_run)
        else:
            res = judge_fields(f, system, args.json, args.dry_run)
    except LLMError as e:
        raise SystemExit(f"judge error: {e}")
    if "dry_run_prompt" in res:
        print("=== system prompt: " + os.path.relpath(args.system_prompt, SKILL_DIR) + " ===\n=== user prompt ===\n" + res["dry_run_prompt"])
    elif "output" in res:
        print(res["output"])
    else:
        print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
