# Cheap edge probe — results

> Generated: 2026-06-07 02:11 · model: `deepseek-v4-pro` · prompt: `probe-v1`

Reduced thesis tested: *does an LLM debate over point-in-time technical features beat a turnover-matched random sizer and Classical-alone, net of cost, across regimes?* Look-ahead-proof by construction (decisions are a pure function of the bounded price slice).


## Verdict: **AMBER**

LLM debate beat random but did not clearly beat Classical-alone in every window — the LLM cost may not be justified. Rethink before investing in the full substrate.


| window | LLM Sharpe | random | classical | beats random? | beats classical? |
|---|---|---|---|---|---|
| bull-grind-oos | 0.268 | -0.032 | 0.83 | ✅ | ❌ |

## Window: bull-grind-oos

_Steady bull grind into 2026; largely post-cutoff -> cleanest out-of-sample read._

Symbols: 10


| strategy | median Sharpe | median total return % |
|---|---|---|
| llm | 0.268 | 2.435 |
| random | -0.032 | -1.42 |
| classical | 0.83 | 4.935 |
| buy_hold | 1.538 | 27.165 |

<details><summary>Per-symbol Sharpe</summary>


| symbol | LLM | random | classical | buy&hold | #decisions |
|---|---|---|---|---|---|
| AAPL | -2.596 | -0.066 | 0.067 | 2.37 | 10 |
| MSFT | -1.015 | 0.041 | -0.378 | -0.585 | 10 |
| NVDA | 1.37 | 0.013 | 0.786 | 0.824 | 10 |
| JPM | 0.254 | -0.021 | 0.156 | 0.354 | 10 |
| XOM | 0.281 | -0.101 | -0.393 | 1.679 | 10 |
| JNJ | 1.145 | 0.124 | 1.858 | 2.565 | 10 |
| WMT | 0.772 | -0.12 | 0.873 | 0.961 | 10 |
| GLD | 0.63 | -0.161 | 1.34 | 1.397 | 10 |
| SPY | -1.605 | -0.042 | 1.222 | 2.023 | 10 |
| QQQ | -0.37 | 0.132 | 1.262 | 2.127 | 10 |

</details>

