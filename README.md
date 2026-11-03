# Streak Pet Bot Eval Agent

Zijun 的 Streak Pet Bot 对话效果评估 skill：单条判断好坏 + 原因、五维打分、人设改写、批量 LLM-as-a-Judge、A/B 对比，以及相对金标集的校准。

Portable skill package for judging Streak Pet Bot replies by Zijun's standard (good/bad + why, 5-dimension scores, persona rewrite, batch LLM-as-judge, A/B compare, golden-set calibration).

## 安装 / Install

把整个文件夹拷进 agent 的 skills 目录即可，例如：

```bash
cp -R streak-pet-bot-eval-agent ~/.cursor/skills/streak-pet-bot-eval-agent
# 或 Claude Code / 其他支持 SKILL.md 的 agent skills 目录
```

不需要访问原 box；所有规则、prompt、金标数据和脚本都在本目录内。

## 环境变量 / Env

```bash
export JUDGE_API_BASE=https://api.openai.com/v1   # 或任何 OpenAI-compatible base；Anthropic 用 .../claude-aws 这类
export JUDGE_API_KEY=sk-...                        # 只从环境读，不会写进任何文件
export JUDGE_MODEL=gpt-4.1                         # 或 claude-sonnet-... / deepseek-chat 等
export JUDGE_API_STYLE=openai                      # openai（默认）| anthropic
# 可选：JUDGE_TEMPERATURE  JUDGE_MAX_TOKENS=2048  JUDGE_TIMEOUT=120  JUDGE_STREAM=0|1  JUDGE_RETRIES=2
```

`JUDGE_API_STYLE=anthropic` 时会 POST `{base}/v1/messages`；默认走 `{base}/v1/chat/completions`。

## 例子 / Examples

```bash
cd streak-pet-bot-eval-agent

# 单条判断（Markdown）
python3 scripts/judge.py --persona sharp_tongued --context "user_b: [分享视频]" \
  --query "@{pet} hi" --reply "Took you long enough."

# 结构化 JSON / 只改写
python3 scripts/judge.py --case-id Q08 --json
python3 scripts/judge.py --case-id Q08 --rewrite-only --json

# 批量（JSONL / CSV / TSV）
python3 scripts/judge.py -i my_cases.jsonl -o judged.jsonl --workers 4

# 相对金标校准
python3 scripts/judge.py --benchmark --limit 5 --report bench.md

# A/B 对比（两列回复 → win/tie/lose + 五维分）
python3 scripts/judge.py --compare -i ab.jsonl -o ab_out.jsonl --col-a reply_a --col-b reply_b

# 不调 API，只看会发出去的 prompt
python3 scripts/judge.py --case-id Q08 --dry-run

# 数据隐私自检
python3 scripts/privacy_check.py data/golden_set.jsonl data/holdout.jsonl
```

## 输入 / 输出

**单条输入字段**（CLI 参数或 JSONL 列）：`persona`（或 `persona.soul_style`）、`mbti`、`bot_name`、`prior_turns`/`context`、`query`、`bot_reply`/`reply`/`case_text`。

**JSON 输出**：
```json
{
  "verdict": "好 | 还可以 | 不太好 | hard_reject | 无法判断",
  "scores": {"A": 2, "B-1": 2.5, "B-2": 2, "C-1": 2, "C-2": 3},
  "weighted_score": 2.35,
  "why": "Zijun 口吻一句话",
  "rewrite_text": "...",
  "rewrite_zh": "..."
}
```

结论规则：好 = 至少一维 3 且其余 ≥2；不太好 = 任一维 ≤1；加权 = `A×0.25 + ((B-1+B-2)/2)×0.35 + ((C-1+C-2)/2)×0.40`（V1）。脚本会本地重算 verdict / weighted，不盲信模型数字。

## 目录

```
SKILL.md
README.md
references/   standards.md  rubric.md  scenario_need_library.md  calibration_lessons.md  eval_data_privacy.md
prompts/      judge_system_prompt.md  rewrite_prompt.md
data/         golden_set.jsonl (23)  holdout.jsonl (8)
scripts/      judge.py  privacy_check.py
```

## 校准反馈 / Report disagreements back to Zijun

发现 judge 和人工不一致时：

1. 把那一条（脱敏后）加进本地 `data/`，字段对齐 `golden_set.jsonl`（含 `zijun_verdict` / `zijun_scores` / `zijun_reason` / `zijun_approved_rewrite`）。
2. 在 `references/calibration_lessons.md` 追加一行「她怎么判 / judge 错在哪 / 学到什么」。
3. 如需改规则，同步更新 `references/standards.md` 和 `prompts/judge_system_prompt.md` 第五节。
4. 把 case + 一两句分歧说明发给 Zijun 确认后再合进主包。

真实私信原文不要外发；改写口径见 `references/eval_data_privacy.md`。

## 许可说明

评测规则与金标结论来自 Zijun；Bot 回复原文保留作被测对象。请勿把未脱敏的线上私信塞进本包或公开仓库。
