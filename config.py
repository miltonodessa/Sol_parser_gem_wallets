import os
from dataclasses import dataclass, field
from typing import Optional, List

from dotenv import load_dotenv

load_dotenv()

HELIUS_API_KEY: str = os.getenv("HELIUS_API_KEY", "")
RPC_URL: str = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HELIUS_API_URL: str = "https://api.helius.xyz/v0"

# GMGN token page URL — {mint} will be replaced with the token mint address
GMGN_TOKEN_URL: str = "https://gmgn.ai/sol/token/{}"

LAMPORTS_PER_SOL: int = 1_000_000_000

# Known DEX program IDs → human-readable names
KNOWN_PROGRAMS: dict = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3sFt2As": "Orca Whirlpool",
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP": "Orca v2",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter v6",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": "Jupiter v4",
    "srmqPvymJeFKQ4zGQed1GFppgkRHL9kaELCbyksJtPX": "Serum",
    "opnb2LAfJYbRMAHHvqjCwQxanZn7ReEHp1k81EohpZb": "OpenBook",
    "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qffwaryk": "Moonshot",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump.fun AMM",
}

# Aggregator program IDs
AGGREGATOR_PROGRAMS: dict = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": "Jupiter",
}

# Wrapped SOL mint
WSOL_MINT: str = "So11111111111111111111111111111111111111112"


@dataclass
class FilterConfig:
    # ── Token SOL Invest ────────────────────────────────────────────────────
    # None means "All"
    min_sol_invest: Optional[float] = None

    # ── Period ──────────────────────────────────────────────────────────────
    # None means "Max" (all-time)
    period_days: Optional[int] = 45

    # ── Performance ─────────────────────────────────────────────────────────
    winrate_min: float = 0.0
    winrate_max: float = 100.0
    pnl_sol_min: Optional[float] = None
    pnl_sol_max: Optional[float] = None

    # ── ROI Settings ────────────────────────────────────────────────────────
    roi_min: Optional[float] = None
    roi_max: Optional[float] = None
    median_roi_min: Optional[float] = None
    median_roi_max: Optional[float] = None
    avg_roi_min: Optional[float] = None
    avg_roi_max: Optional[float] = None

    # ── Balances ────────────────────────────────────────────────────────────
    total_sol_min: Optional[float] = None
    total_sol_max: Optional[float] = None
    total_usd_min: Optional[float] = None
    total_usd_max: Optional[float] = None

    # ── Trading ─────────────────────────────────────────────────────────────
    trade_providers: List[str] = field(default_factory=list)
    pool_providers: List[str] = field(default_factory=list)
    pool_type: Optional[str] = None
    pool_type_mode: str = "count"        # "count" | "percent"
    aggregators: List[str] = field(default_factory=list)
    aggregators_mode: str = "count"      # "count" | "percent"

    # ── Last / First Trade ──────────────────────────────────────────────────
    # Format: "1d2h30m10s"  (max > min)
    last_trade_min: Optional[str] = None
    last_trade_max: Optional[str] = None
    first_trade_min: Optional[str] = None
    first_trade_max: Optional[str] = None

    # ── Total Trades ────────────────────────────────────────────────────────
    total_trades_min: Optional[int] = None
    total_trades_max: Optional[int] = None

    # ── Rockets count ───────────────────────────────────────────────────────
    rockets_x2: bool = False    # at least one token with ≥ x2 ROI
    rockets_x5: bool = False    # at least one token with ≥ x5 ROI
    rockets_x10: bool = False   # at least one token with ≥ x10 ROI

    # ── Trade Duration ──────────────────────────────────────────────────────
    trade_duration_min: Optional[int] = None   # seconds
    trade_duration_max: Optional[int] = None   # seconds

    # ── Global toggle ───────────────────────────────────────────────────────
    active_filters: bool = True
