# Cheap edge probe — results

> Generated: 2026-06-07 00:35 · model: `deepseek-v4-pro` · prompt: `probe-v1`

Reduced thesis tested: *does an LLM debate over point-in-time technical features beat a turnover-matched random sizer and Classical-alone, net of cost, across regimes?* Look-ahead-proof by construction (decisions are a pure function of the bounded price slice).


## Verdict: **AMBER**

LLM debate beat random but did not clearly beat Classical-alone in every window — the LLM cost may not be justified. Rethink before investing in the full substrate.


| window | LLM Sharpe | random | classical | beats random? | beats classical? |
|---|---|---|---|---|---|
| tariff-shock-drawdown | -0.229 | -0.229 | 1.003 | ❌ | ❌ |

## Window: tariff-shock-drawdown

_Apr-2025 'Liberation Day' tariff crash + V-recovery; high-vol stress; partly in-sample._

Symbols: 1


| strategy | median Sharpe | median total return % |
|---|---|---|
| llm | -0.229 | -0.91 |
| random | -0.229 | -0.91 |
| classical | 1.003 | 2.26 |
| buy_hold | -0.275 | -8.54 |

<details><summary>Per-symbol Sharpe</summary>


| symbol | LLM | random | classical | buy&hold | #decisions |
|---|---|---|---|---|---|
| AAPL | -0.229 | -0.229 | 1.003 | -0.275 | 6 |

</details>

