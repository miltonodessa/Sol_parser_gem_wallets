"""
filters.py — applies the FilterConfig criteria to a list of WalletStats.
"""

import re
import time
from typing import List

from config import FilterConfig
from src.analyzer import WalletStats


# ── duration string parser ─────────────────────────────────────────────────

_DURATION_RE = re.compile(
    r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?",
    re.IGNORECASE,
)


def parse_duration(s: str) -> int:
    """
    Convert a human-readable duration string to seconds.
    Supported formats: "7d", "1d2h30m10s", "3h", "45m", "90s".
    Returns 0 for empty / unrecognised input.
    """
    if not s or not s.strip():
        return 0
    m = _DURATION_RE.match(s.strip())
    if not m or not any(m.groups()):
        return 0
    d, h, mi, sec = (int(g or 0) for g in m.groups())
    return d * 86_400 + h * 3_600 + mi * 60 + sec


def seconds_to_human(seconds: float) -> str:
    """
    Convert seconds to a human-readable string like "3d 14h 25m 10s".
    Used both in exports and in the filter module itself.
    """
    seconds = int(seconds)
    if seconds <= 0:
        return "0s"
    parts = []
    if seconds >= 86_400:
        parts.append(f"{seconds // 86_400}d")
        seconds %= 86_400
    if seconds >= 3_600:
        parts.append(f"{seconds // 3_600}h")
        seconds %= 3_600
    if seconds >= 60:
        parts.append(f"{seconds // 60}m")
        seconds %= 60
    if seconds:
        parts.append(f"{seconds}s")
    return " ".join(parts)


# ── filter engine ─────────────────────────────────────────────────────────────

class FilterEngine:

    def apply(
        self,
        wallets: List[WalletStats],
        cfg: FilterConfig,
    ) -> List[WalletStats]:
        """Return only those wallets that satisfy every active criterion."""
        if not cfg.active_filters:
            return wallets
        return [w for w in wallets if self._passes(w, cfg)]

    # ── private ───────────────────────────────────────────────────────────

    @staticmethod
    def _in_range(value: float, lo, hi) -> bool:
        if lo is not None and value < lo:
            return False
        if hi is not None and value > hi:
            return False
        return True

    def _passes(self, w: WalletStats, cfg: FilterConfig) -> bool:  # noqa: C901
        now = int(time.time())

        # ── Performance ──────────────────────────────────────────────────
        winrate_pct = w.winrate * 100
        if not self._in_range(winrate_pct, cfg.winrate_min, cfg.winrate_max):
            return False

        if not self._in_range(w.pnl_sol, cfg.pnl_sol_min, cfg.pnl_sol_max):
            return False

        # ── ROI ──────────────────────────────────────────────────────────
        if not self._in_range(w.roi, cfg.roi_min, cfg.roi_max):
            return False
        if not self._in_range(w.median_roi, cfg.median_roi_min, cfg.median_roi_max):
            return False
        if not self._in_range(w.avg_roi, cfg.avg_roi_min, cfg.avg_roi_max):
            return False

        # ── Balances ─────────────────────────────────────────────────────
        if not self._in_range(w.sol_balance, cfg.total_sol_min, cfg.total_sol_max):
            return False
        if not self._in_range(w.usd_balance, cfg.total_usd_min, cfg.total_usd_max):
            return False

        # ── Total Trades ─────────────────────────────────────────────────
        if not self._in_range(w.total_trades, cfg.total_trades_min, cfg.total_trades_max):
            return False

        # ── Last Trade ───────────────────────────────────────────────────
        if w.last_swap_ts:
            elapsed = now - w.last_swap_ts
            if cfg.last_trade_min:
                if elapsed < parse_duration(cfg.last_trade_min):
                    return False
            if cfg.last_trade_max:
                if elapsed > parse_duration(cfg.last_trade_max):
                    return False
        elif cfg.last_trade_min or cfg.last_trade_max:
            return False   # no data but filter requested

        # ── First Trade ──────────────────────────────────────────────────
        if w.first_swap_ts:
            age = now - w.first_swap_ts
            if cfg.first_trade_min:
                if age < parse_duration(cfg.first_trade_min):
                    return False
            if cfg.first_trade_max:
                if age > parse_duration(cfg.first_trade_max):
                    return False

        # ── Rockets ──────────────────────────────────────────────────────
        if cfg.rockets_x2  and w.rockets_x2  == 0:
            return False
        if cfg.rockets_x5  and w.rockets_x5  == 0:
            return False
        if cfg.rockets_x10 and w.rockets_x10 == 0:
            return False

        # ── Trade Providers ──────────────────────────────────────────────
        if cfg.trade_providers:
            if not any(p in w.trade_providers for p in cfg.trade_providers):
                return False

        # ── Aggregators ──────────────────────────────────────────────────
        if cfg.aggregators:
            wallet_aggs = list(w.aggregator_counts.keys())
            if not any(a in wallet_aggs for a in cfg.aggregators):
                return False

        # ── Pool Type ────────────────────────────────────────────────────
        if cfg.pool_type:
            if cfg.pool_type not in w.pool_type_counts:
                return False

        # ── Trade Duration ───────────────────────────────────────────────
        if not self._in_range(
            w.avg_trade_duration,
            cfg.trade_duration_min,
            cfg.trade_duration_max,
        ):
            return False

        return True
