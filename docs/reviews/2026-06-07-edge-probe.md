# Cheap edge probe — results

> Generated: 2026-06-07 01:36 · model: `deepseek-v4-pro` · prompt: `probe-v1`

Reduced thesis tested: *does an LLM debate over point-in-time technical features beat a turnover-matched random sizer and Classical-alone, net of cost, across regimes?* Look-ahead-proof by construction (decisions are a pure function of the bounded price slice).


## Verdict: **KILL**

LLM debate failed to beat a turnover-matched random sizer (same names, same trade count, same cost) in at least one regime — no evidence of skill over luck.


| window | LLM Sharpe | random | classical | beats random? | beats classical? |
|---|---|---|---|---|---|
| tariff-shock-drawdown | -0.071 | -0.019 | 0.0 | ❌ | ❌ |
| bull-grind-oos | -0.497 | 0.052 | 0.83 | ❌ | ❌ |

## Window: tariff-shock-drawdown

_Apr-2025 'Liberation Day' tariff crash + V-recovery; high-vol stress; partly in-sample._

Symbols: 10


| strategy | median Sharpe | median total return % |
|---|---|---|
| llm | -0.071 | -0.55 |
| random | -0.019 | -0.765 |
| classical | 0.0 | 0.0 |
| buy_hold | 0.811 | 9.85 |

<details><summary>Per-symbol Sharpe</summary>


| symbol | LLM | random | classical | buy&hold | #decisions |
|---|---|---|---|---|---|
| AAPL | -0.229 | -0.229 | 1.003 | -0.275 | 6 |
| MSFT | -2.083 | -0.457 | 0.0 | 2.084 | 6 |
| NVDA | -0.267 | 0.019 | 0.019 | 1.865 | 6 |
| JPM | 0.313 | 0.31 | 0.489 | 0.866 | 6 |
| XOM | 0.313 | -0.055 | 0.0 | 0.598 | 6 |
| JNJ | 1.412 | -1.453 | 0.0 | 1.022 | 6 |
| WMT | -0.556 | 0.179 | -0.012 | 0.025 | 6 |
| GLD | -0.165 | 0.039 | 1.541 | 1.581 | 6 |
| SPY | 0.334 | -0.524 | 0.0 | 0.62 | 6 |
| QQQ | 0.023 | 0.018 | 0.0 | 0.756 | 6 |

</details>


## Window: bull-grind-oos

_Steady bull grind into 2026; largely post-cutoff -> cleanest out-of-sample read._

Symbols: 10


| strategy | median Sharpe | median total return % |
|---|---|---|
| llm | -0.497 | -2.815 |
| random | 0.052 | 0.015 |
| classical | 0.83 | 4.935 |
| buy_hold | 1.538 | 27.165 |

<details><summary>Per-symbol Sharpe</summary>


| symbol | LLM | random | classical | buy&hold | #decisions |
|---|---|---|---|---|---|
| AAPL | -1.061 | 0.04 | 0.067 | 2.37 | 10 |
| MSFT | -0.412 | 0.339 | -0.378 | -0.585 | 10 |
| NVDA | 0.674 | -0.359 | 0.786 | 0.824 | 10 |
| JPM | -0.043 | -0.043 | 0.156 | 0.354 | 10 |
| XOM | -0.889 | 0.064 | -0.393 | 1.679 | 10 |
| JNJ | -1.569 | 0.165 | 1.858 | 2.565 | 10 |
| WMT | -0.663 | 0.546 | 0.873 | 0.961 | 10 |
| GLD | 0.498 | 0.008 | 1.34 | 1.397 | 10 |
| SPY | 0.84 | 0.032 | 1.222 | 2.023 | 10 |
| QQQ | -0.582 | 0.114 | 1.262 | 2.127 | 10 |

</details>

