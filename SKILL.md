---
name: streak-pet-bot-eval-agent
description: >-
  Judge Streak Pet Bot replies by Zijun's standard: good/bad + why, 5-dimension
  scores (A/B-1/B-2/C-1/C-2), persona rewriting, batch LLM-as-a-judge, A/B or
  GSB-style model/prompt comparison, and golden-set calibration. Use when
  scoring, rewriting, or benchmarking Streak Pet Bot dialogue quality.
---

# Streak Pet Bot Eval Agent（Zijun 标准）

完整、可移植的对话效果评估 skill。规则、prompt、脱敏金标和 CLI 都在本目录；不依赖原 box。

## 什么时候用

- 单条回复：好不好、为什么、五维几分、怎么改写才像这个人设。
- 批量：JSONL / CSV / TSV 跑 LLM-as-a-Judge。
- A/B：两个模型或两版 prompt 的回复，出 win / tie / lose + 分维度对比（GSB 风格）。
- 校准：拿 judge 去打 `data/golden_set.jsonl`，看和 Zijun 亲评的一致率。
- 扩库：按隐私规则脱敏后加新 case，并写入校准教训。

## 端到端流程

1. **读规则**：`references/standards.md`（全量判准）+ `references/rubric.md`（五维锚点）+ `references/scenario_need_library.md`（G1–G5 / S01–S15）。
2. **单条判断 → 打分 → 改写**
   ```bash
   python3 scripts/judge.py --persona sharp_tongued \
     --context "user_b: [分享视频]" --query "@{pet} hi" \
     --reply "Took you long enough." --json
   ```
   或 `--case-id Q08` 从金标加载；`--rewrite-only` 只写最佳回复。
3. **批量**
   ```bash
   python3 scripts/judge.py -i cases.jsonl -o judged.jsonl --workers 4
   ```
4. **A/B / GSB**
   ```bash
   python3 scripts/judge.py --compare -i ab.jsonl -o ab_out.jsonl \
     --col-a reply_a --col-b reply_b
   ```
   默认加权分差 ≤0.15 算 tie；汇总 A_win / tie / B_win、好/差率、分维度均值。
5. **校准 judge**
   ```bash
   python3 scripts/judge.py --benchmark [--limit N] [--ids Q08,Q14] --report bench.md
   ```
   指标：三档结论一致率、好/差二分类一致率、加权 ±0.5 命中率、五维 MAE。
6. **加新校准 case**：按 `references/eval_data_privacy.md` 脱敏 → 写入 `data/*.jsonl` → 用 `scripts/privacy_check.py` 扫一遍 → 把分歧记进 `references/calibration_lessons.md` → 必要时更新 `prompts/judge_system_prompt.md` 第五节 → 交给 Zijun 确认。

上下文不够时允许 `无法判断`；`--dry-run` 只打印 prompt，不调 API。

## 输出格式

JSON（`--json` / 批量默认）：

| 字段 | 含义 |
|---|---|
| `verdict` | `好` / `还可以` / `不太好` / `hard_reject` / `无法判断` |
| `scores` | `{A, B-1, B-2, C-1, C-2}`，各 0–3 |
| `weighted_score` | `A×0.25 + ((B-1+B-2)/2)×0.35 + ((C-1+C-2)/2)×0.40`（V1） |
| `why` | Zijun 口吻一句话（约 20 字） |
| `rewrite_text` / `rewrite_zh` | 1–2 行最佳回复 + 中文 |

结论硬规则（脚本本地重算）：**好** = 至少一维 =3 且其余 ≥2；**不太好** = 任一维 ≤1；命中底线闸门 → `hard_reject`。

## 环境

```bash
export JUDGE_API_BASE=...   # OpenAI-compatible 或 Anthropic Messages base
export JUDGE_API_KEY=...    # 只读环境，不落盘
export JUDGE_MODEL=...
export JUDGE_API_STYLE=openai   # 或 anthropic
```

依赖：Python 3.8+ 标准库（`urllib`）。无第三方包。

## 参考文件

| 路径 | 内容 |
|---|---|
| `references/standards.md` | 全量判断规则（人设、产品机制、主动发言、写作规范） |
| `references/rubric.md` | 五维 0–3 锚点、结论规则、输出模板 |
| `references/scenario_need_library.md` | G1–G5 需求簇 × S01–S15 场景（脱敏比例） |
| `references/calibration_lessons.md` | Q1–Q24 / holdout R1 / GSB 坑 |
| `references/eval_data_privacy.md` | 评测数据怎么脱敏 |
| `prompts/judge_system_prompt.md` | Judge 系统 prompt（含 R-01..R-25 + 补充规则） |
| `prompts/rewrite_prompt.md` | 只改写时的系统 prompt |
| `data/golden_set.jsonl` | 23 条 Zijun 亲评金标（已脱敏） |
| `data/holdout.jsonl` | 8 条 holdout（已脱敏） |
| `scripts/judge.py` | 单条 / 批量 / benchmark / compare / dry-run |
| `scripts/privacy_check.py` | 数据隐私自检 |

人设代码：`sharp_tongued`（毒舌）/ `gentle`（温柔）/ `tsundere`（傲娇）/ `cool`（高冷=霸总味）。文中一律写全称 **Streak Pet Bot**，不要缩写成 SPB。
