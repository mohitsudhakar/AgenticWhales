"""Curated ticker universe for batch analysis.

Symbols use Yahoo Finance conventions (futures use `=F`, FX uses `=X`,
crypto uses `-USD`). Index ETFs are preferred over `^`-prefixed indexes
in the default selection because the data layer handles ETFs more
reliably across providers.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# Each entry: (symbol, display_name)
TickerEntry = Tuple[str, str]

UNIVERSE: Dict[str, List[TickerEntry]] = {
    "Indexes": [
        ("^GSPC", "S&P 500"),
        ("^DJI", "Dow Jones"),
        ("^IXIC", "Nasdaq Composite"),
        ("^NDX", "Nasdaq-100"),
        ("^RUT", "Russell 2000"),
        ("^VIX", "VIX"),
        ("^FTSE", "FTSE 100 (UK)"),
        ("^N225", "Nikkei 225 (JP)"),
        ("^HSI", "Hang Seng (HK)"),
        ("^STOXX50E", "Euro Stoxx 50"),
    ],
    "Index & Sector ETFs": [
        ("SPY", "S&P 500 ETF"),
        ("QQQ", "Nasdaq-100 ETF"),
        ("DIA", "Dow ETF"),
        ("IWM", "Russell 2000 ETF"),
        ("VTI", "Total US Market"),
        ("EFA", "Developed ex-US"),
        ("EEM", "Emerging Markets"),
        ("VWO", "Emerging Markets (Vanguard)"),
        ("XLF", "Financials sector"),
        ("XLK", "Technology sector"),
        ("XLV", "Healthcare sector"),
        ("XLE", "Energy sector"),
        ("XLI", "Industrials sector"),
        ("XLY", "Consumer Discretionary"),
        ("XLP", "Consumer Staples"),
        ("XLU", "Utilities"),
    ],
    "Mega-cap Stocks": [
        ("AAPL", "Apple"),
        ("MSFT", "Microsoft"),
        ("NVDA", "Nvidia"),
        ("GOOGL", "Alphabet"),
        ("AMZN", "Amazon"),
        ("META", "Meta"),
        ("TSLA", "Tesla"),
        ("AVGO", "Broadcom"),
        ("BRK-B", "Berkshire Hathaway"),
        ("JPM", "JPMorgan"),
        ("V", "Visa"),
        ("MA", "Mastercard"),
        ("UNH", "UnitedHealth"),
        ("XOM", "ExxonMobil"),
        ("CVX", "Chevron"),
        ("WMT", "Walmart"),
        ("LLY", "Eli Lilly"),
        ("ABBV", "AbbVie"),
        ("COST", "Costco"),
        ("HD", "Home Depot"),
        ("ORCL", "Oracle"),
        ("NFLX", "Netflix"),
        ("CSCO", "Cisco"),
        ("BAC", "Bank of America"),
        ("KO", "Coca-Cola"),
    ],
    "Mid/Small-cap (high volume)": [
        # Tech / SaaS
        ("AMD", "AMD"),
        ("PLTR", "Palantir"),
        ("SHOP", "Shopify"),
        ("SNOW", "Snowflake"),
        ("NET", "Cloudflare"),
        ("CRWD", "CrowdStrike"),
        ("DDOG", "Datadog"),
        ("ZS", "Zscaler"),
        ("OKTA", "Okta"),
        ("MDB", "MongoDB"),
        ("U", "Unity"),
        # Consumer / Internet
        ("ROKU", "Roku"),
        ("SNAP", "Snap"),
        ("PINS", "Pinterest"),
        ("RBLX", "Roblox"),
        ("UBER", "Uber"),
        ("ABNB", "Airbnb"),
        ("DASH", "DoorDash"),
        ("DKNG", "DraftKings"),
        # Fintech / consumer credit
        ("COIN", "Coinbase"),
        ("HOOD", "Robinhood"),
        ("SOFI", "SoFi"),
        ("AFRM", "Affirm"),
        # EV / autos
        ("RIVN", "Rivian"),
        ("LCID", "Lucid"),
        ("NIO", "NIO"),
        ("XPEV", "XPeng"),
        ("LI", "Li Auto"),
        # China ADRs
        ("BABA", "Alibaba"),
        ("JD", "JD.com"),
        ("PDD", "PDD Holdings"),
        # Crypto-leveraged equities
        ("MARA", "Marathon Digital"),
        ("RIOT", "Riot Platforms"),
        # Meme / very-high-volume
        ("CVNA", "Carvana"),
        ("GME", "GameStop"),
        ("AMC", "AMC Entertainment"),
    ],
    "Energy": [
        # Futures
        ("CL=F", "WTI Crude Oil"),
        ("BZ=F", "Brent Crude"),
        ("NG=F", "Natural Gas"),
        ("HO=F", "Heating Oil"),
        ("RB=F", "RBOB Gasoline"),
        # ETFs
        ("USO", "US Oil Fund (WTI ETF)"),
        ("BNO", "Brent Oil Fund ETF"),
        ("UNG", "US Natural Gas Fund"),
        ("XLE", "Energy Sector ETF"),
        ("XOP", "Oil & Gas E&P ETF"),
    ],
    "Metals": [
        # Futures
        ("GC=F", "Gold"),
        ("SI=F", "Silver"),
        ("PL=F", "Platinum"),
        ("PA=F", "Palladium"),
        ("HG=F", "Copper"),
        # ETFs
        ("GLD", "Gold ETF (SPDR)"),
        ("IAU", "Gold ETF (iShares)"),
        ("SLV", "Silver ETF"),
        ("CPER", "Copper ETF"),
        ("GDX", "Gold Miners ETF"),
        ("GDXJ", "Junior Gold Miners ETF"),
    ],
    "Agriculture": [
        ("ZC=F", "Corn"),
        ("ZW=F", "Wheat"),
        ("ZS=F", "Soybeans"),
        ("ZL=F", "Soybean Oil"),
        ("ZM=F", "Soybean Meal"),
        ("KC=F", "Coffee"),
        ("CC=F", "Cocoa"),
        ("SB=F", "Sugar"),
        ("CT=F", "Cotton"),
        ("LE=F", "Live Cattle"),
        ("HE=F", "Lean Hogs"),
    ],
    "Crypto": [
        ("BTC-USD", "Bitcoin"),
        ("ETH-USD", "Ethereum"),
        ("SOL-USD", "Solana"),
        ("XRP-USD", "XRP"),
        ("DOGE-USD", "Dogecoin"),
        ("ADA-USD", "Cardano"),
        ("AVAX-USD", "Avalanche"),
        ("IBIT", "BlackRock Bitcoin ETF"),
        ("MSTR", "MicroStrategy (BTC proxy)"),
    ],
    "FX": [
        ("EURUSD=X", "EUR/USD"),
        ("GBPUSD=X", "GBP/USD"),
        ("USDJPY=X", "USD/JPY"),
        ("AUDUSD=X", "AUD/USD"),
        ("USDCAD=X", "USD/CAD"),
        ("USDCHF=X", "USD/CHF"),
        ("NZDUSD=X", "NZD/USD"),
        ("USDCNY=X", "USD/CNY"),
        ("USDMXN=X", "USD/MXN"),
        ("USDINR=X", "USD/INR"),
        ("DX=F", "US Dollar Index"),
    ],
}


def all_symbols() -> List[str]:
    """Flat list of every symbol in the universe."""
    return [sym for entries in UNIVERSE.values() for sym, _ in entries]


def universe_for_api() -> List[Dict[str, object]]:
    """Shape the universe for JSON serialization in /api/config."""
    return [
        {
            "category": cat,
            "tickers": [{"symbol": s, "name": n} for s, n in entries],
        }
        for cat, entries in UNIVERSE.items()
    ]
