"""
analyzer.py — parses raw Helius transactions and computes per-wallet metrics
that match the Excel schema used by the existing wallets_base file.

Column mapping (same as existing Excel):
  A  Wallet
  B  SOL Balance
  C  USD Balance
  D  Winrate
  E  ROI
  F  Median ROI
  G  AVG ROI
  H  PNL SOL
  I  Spent SOL
  J  Earned SOL
  K  SMTB SPL          (avg SOL spent per token = Spent / Tokens)
  L  Tokens
  M  AVG Buys
  N  AVG Sells
  O  AVG Sell %
  P  Median Sell %
  Q  AVG SPL Holdings  (avg unrealised token holding in SOL)
  R  Holding positions (# tokens still held)
  S  Median Trade Duration
  T  AVG Trade Duration
  U  Avg Duration to First Sell
  V  Last Swap         (Excel serial date)
  W  First Swap        (Excel serial date)
  X  AVG Swap Fee SOL
  Y  Median AVG Swap Fee SOL
  Z  Multi Trans Swaps %
"""

import statistics
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import config as cfg


# ── per-token trade record ────────────────────────────────────────────────────

@dataclass
class TokenTrade:
    token_mint: str
    buy_sol: float          # total SOL spent buying
    sell_sol: float         # total SOL received selling
    pnl_sol: float          # sell_sol - buy_sol
    roi_pct: float          # pnl / buy * 100  (0 if buy == 0)
    buy_count: int
    sell_count: int
    sell_pct: float         # sell_count / (buy_count + sell_count)  0-1
    first_trade_ts: int     # unix timestamp of earliest swap
    last_trade_ts: int      # unix timestamp of latest swap
    trade_duration: int     # last - first  (seconds)
    time_to_first_sell: int # first sell ts - first buy ts  (seconds; 0 if no sell)
    is_profitable: bool
    multi_tx: bool          # had more than 1 tx per token → contributes to multi_tx%
    fees_sol: List[float]   # per-tx fees
    dexes: List[str]
    pools: List[str]
    aggregators: List[str]
    gmgn_url: str


# ── wallet-level stats ────────────────────────────────────────────────────────

@dataclass
class WalletStats:
    address: str

    # balances
    sol_balance: float
    usd_balance: float

    # performance
    winrate: float          # 0-1
    roi: float              # overall ROI %
    median_roi: float
    avg_roi: float
    pnl_sol: float
    spent_sol: float
    earned_sol: float

    # per-token averages
    smtb_spl: float         # avg SOL spent per token
    tokens_count: int
    avg_buys: float
    avg_sells: float
    avg_sell_pct: float
    median_sell_pct: float
    avg_spl_holdings: float  # avg unrealised holding (SOL value; 0 if no price)
    holding_positions: int   # # tokens not fully sold

    # durations (seconds)
    median_trade_duration: float
    avg_trade_duration: float
    avg_duration_to_first_sell: float

    # time
    last_swap_ts: int
    first_swap_ts: int

    # fees
    avg_swap_fee_sol: float
    median_avg_swap_fee_sol: float

    # misc
    multi_trans_swaps_pct: float   # 0-1

    # rockets
    rockets_x2: int
    rockets_x5: int
    rockets_x10: int

    # provider info
    trade_providers: List[str]
    pool_providers: List[str]
    pool_type_counts: Dict[str, int]
    aggregator_counts: Dict[str, int]

    # total raw swap count (for filtering by total_trades)
    total_trades: int

    # per-token detail (for extended Excel sheet + GMGN links)
    token_trades: List[TokenTrade] = field(default_factory=list)


# ── analyzer ─────────────────────────────────────────────────────────────────

class WalletAnalyzer:

    def _dex_from_tx(self, tx: dict) -> str:
        for ix in tx.get("instructions", []):
            prog = ix.get("programId", "")
            if prog in cfg.KNOWN_PROGRAMS:
                return cfg.KNOWN_PROGRAMS[prog]
        for acc in tx.get("accountData", []):
            prog = acc.get("account", "")
            if prog in cfg.KNOWN_PROGRAMS:
                return cfg.KNOWN_PROGRAMS[prog]
        src = tx.get("source", "") or ""
        return src if src else "Unknown"

    def _aggregator_from_tx(self, tx: dict) -> str:
        for ix in tx.get("instructions", []):
            prog = ix.get("programId", "")
            if prog in cfg.AGGREGATOR_PROGRAMS:
                return cfg.AGGREGATOR_PROGRAMS[prog]
        return ""

    def _fee_sol(self, tx: dict) -> float:
        fee = tx.get("fee", 0) or 0
        return fee / cfg.LAMPORTS_PER_SOL

    def _parse_swap(self, tx: dict) -> Optional[dict]:
        """
        Extract buy/sell direction, token mint, and SOL amount from a
        Helius Enhanced SWAP transaction.
        Returns None when the transaction cannot be parsed as a SOL↔token swap.
        """
        if tx.get("type") != "SWAP":
            return None

        swap = tx.get("events", {}).get("swap") or {}

        native_input  = swap.get("nativeInput")
        native_output = swap.get("nativeOutput")
        token_inputs  = swap.get("tokenInputs", [])
        token_outputs = swap.get("tokenOutputs", [])

        result = {
            "signature": tx.get("signature"),
            "timestamp": tx.get("timestamp", 0),
            "is_buy": None,
            "sol_amount": 0.0,
            "token_mint": None,
            "dex": self._dex_from_tx(tx),
            "aggregator": self._aggregator_from_tx(tx),
            "fee_sol": self._fee_sol(tx),
        }

        # ── buy: SOL in, token out ────────────────────────────────────────
        if native_input and token_outputs:
            result["is_buy"] = True
            result["sol_amount"] = native_input.get("amount", 0) / cfg.LAMPORTS_PER_SOL
            result["token_mint"] = token_outputs[0].get("mint")

        # ── sell: token in, SOL out ───────────────────────────────────────
        elif native_output and token_inputs:
            result["is_buy"] = False
            result["sol_amount"] = native_output.get("amount", 0) / cfg.LAMPORTS_PER_SOL
            result["token_mint"] = token_inputs[0].get("mint")

        # ── token-to-token (ignore) ───────────────────────────────────────
        else:
            return None

        if not result["token_mint"] or result["sol_amount"] <= 0:
            return None

        return result

    def analyze_wallet(
        self,
        address: str,
        raw_txs: List[dict],
        sol_balance: float,
        sol_price_usd: float,
        min_sol_invest: Optional[float] = None,
    ) -> WalletStats:
        """
        Build WalletStats from raw Helius transactions.
        *min_sol_invest* filters out tokens where total buy < threshold.
        """
        # ── group swaps by token ──────────────────────────────────────────
        by_token: Dict[str, Dict] = {}

        for tx in raw_txs:
            parsed = self._parse_swap(tx)
            if not parsed or not parsed["token_mint"]:
                continue

            mint = parsed["token_mint"]
            if mint == cfg.WSOL_MINT:
                continue   # skip wrapped SOL ↔ native SOL

            if mint not in by_token:
                by_token[mint] = {
                    "buys":  [],   # (sol, ts)
                    "sells": [],   # (sol, ts)
                    "dexes": [],
                    "pools": [],
                    "aggregators": [],
                    "fees": [],
                    "tx_count": 0,
                }

            td = by_token[mint]
            td["tx_count"] += 1
            td["fees"].append(parsed["fee_sol"])

            if parsed["dex"] and parsed["dex"] != "Unknown":
                td["dexes"].append(parsed["dex"])
            if parsed["aggregator"]:
                td["aggregators"].append(parsed["aggregator"])

            if parsed["is_buy"]:
                td["buys"].append((parsed["sol_amount"], parsed["timestamp"]))
            else:
                td["sells"].append((parsed["sol_amount"], parsed["timestamp"]))

        # ── compute per-token metrics ─────────────────────────────────────
        token_trades: List[TokenTrade] = []
        all_dexes: List[str] = []
        all_pools: List[str] = []
        all_aggregators: List[str] = []
        pool_type_counts: Dict[str, int] = {}
        aggregator_counts: Dict[str, int] = {}

        for mint, td in by_token.items():
            total_buy  = sum(b[0] for b in td["buys"])
            total_sell = sum(s[0] for s in td["sells"])

            # apply min SOL invest filter per token
            if min_sol_invest is not None and total_buy < min_sol_invest:
                continue

            pnl = total_sell - total_buy
            roi = (pnl / total_buy * 100) if total_buy > 0 else 0.0

            all_times = [t for _, t in td["buys"]] + [t for _, t in td["sells"]]
            first_ts  = min(all_times) if all_times else 0
            last_ts   = max(all_times) if all_times else 0
            duration  = last_ts - first_ts

            buy_ts_list  = sorted(t for _, t in td["buys"])
            sell_ts_list = sorted(t for _, t in td["sells"])
            first_sell_ts = sell_ts_list[0] if sell_ts_list else 0
            first_buy_ts  = buy_ts_list[0]  if buy_ts_list  else 0
            time_to_first_sell = (
                max(0, first_sell_ts - first_buy_ts) if first_sell_ts and first_buy_ts else 0
            )

            total_tx = len(td["buys"]) + len(td["sells"])
            sell_pct  = len(td["sells"]) / total_tx if total_tx else 0.0
            multi_tx  = td["tx_count"] > 1

            dex_list = list(set(td["dexes"]))
            agg_list = list(set(td["aggregators"]))

            all_dexes.extend(dex_list)
            all_aggregators.extend(agg_list)

            for dex in dex_list:
                pool_type_counts[dex] = pool_type_counts.get(dex, 0) + 1
            for agg in agg_list:
                aggregator_counts[agg] = aggregator_counts.get(agg, 0) + 1

            is_profitable = pnl > 0

            token_trades.append(TokenTrade(
                token_mint=mint,
                buy_sol=total_buy,
                sell_sol=total_sell,
                pnl_sol=pnl,
                roi_pct=roi,
                buy_count=len(td["buys"]),
                sell_count=len(td["sells"]),
                sell_pct=sell_pct,
                first_trade_ts=first_ts,
                last_trade_ts=last_ts,
                trade_duration=duration,
                time_to_first_sell=time_to_first_sell,
                is_profitable=is_profitable,
                multi_tx=multi_tx,
                fees_sol=td["fees"],
                dexes=dex_list,
                pools=td["pools"],
                aggregators=agg_list,
                gmgn_url=cfg.GMGN_TOKEN_URL.format(mint),
            ))

        # ── empty wallet guard ────────────────────────────────────────────
        if not token_trades:
            return WalletStats(
                address=address,
                sol_balance=sol_balance,
                usd_balance=sol_balance * sol_price_usd,
                winrate=0, roi=0, median_roi=0, avg_roi=0,
                pnl_sol=0, spent_sol=0, earned_sol=0,
                smtb_spl=0, tokens_count=0,
                avg_buys=0, avg_sells=0,
                avg_sell_pct=0, median_sell_pct=0,
                avg_spl_holdings=0, holding_positions=0,
                median_trade_duration=0, avg_trade_duration=0,
                avg_duration_to_first_sell=0,
                last_swap_ts=0, first_swap_ts=0,
                avg_swap_fee_sol=0, median_avg_swap_fee_sol=0,
                multi_trans_swaps_pct=0,
                rockets_x2=0, rockets_x5=0, rockets_x10=0,
                trade_providers=[], pool_providers=[],
                pool_type_counts={}, aggregator_counts={},
                total_trades=0, token_trades=[],
            )

        # ── aggregate ─────────────────────────────────────────────────────
        profitable = [t for t in token_trades if t.is_profitable]
        winrate    = len(profitable) / len(token_trades)

        all_rois   = [t.roi_pct for t in token_trades]
        median_roi = statistics.median(all_rois)
        avg_roi    = statistics.mean(all_rois)

        spent_sol  = sum(t.buy_sol  for t in token_trades)
        earned_sol = sum(t.sell_sol for t in token_trades)
        pnl_sol    = earned_sol - spent_sol
        roi_overall = (pnl_sol / spent_sol * 100) if spent_sol else 0.0

        smtb_spl   = spent_sol / len(token_trades) if token_trades else 0.0

        avg_buys   = statistics.mean(t.buy_count  for t in token_trades)
        avg_sells  = statistics.mean(t.sell_count for t in token_trades)

        sell_pcts  = [t.sell_pct for t in token_trades]
        avg_sell_pct    = statistics.mean(sell_pcts)
        median_sell_pct = statistics.median(sell_pcts)

        holding_positions = sum(
            1 for t in token_trades if t.buy_sol > t.sell_sol
        )
        # avg_spl_holdings — unrealised position value; we don't have live
        # token prices so we approximate with (buy_sol - sell_sol) for open positions
        avg_spl_holdings = statistics.mean(
            max(0.0, t.buy_sol - t.sell_sol) for t in token_trades
        )

        durations = [t.trade_duration for t in token_trades if t.trade_duration > 0]
        median_td = statistics.median(durations) if durations else 0.0
        avg_td    = statistics.mean(durations)   if durations else 0.0

        ttfs_list = [t.time_to_first_sell for t in token_trades if t.time_to_first_sell > 0]
        avg_ttfs  = statistics.mean(ttfs_list) if ttfs_list else 0.0

        all_ts = (
            [t.first_trade_ts for t in token_trades if t.first_trade_ts]
            + [t.last_trade_ts  for t in token_trades if t.last_trade_ts]
        )
        first_swap_ts = min(all_ts) if all_ts else 0
        last_swap_ts  = max(all_ts) if all_ts else 0

        all_fees = [f for t in token_trades for f in t.fees_sol if f > 0]
        avg_fee    = statistics.mean(all_fees)   if all_fees else 0.0
        median_fee = statistics.median(all_fees) if all_fees else 0.0

        multi_count = sum(1 for t in token_trades if t.multi_tx)
        multi_pct   = multi_count / len(token_trades) if token_trades else 0.0

        rockets_x2  = sum(1 for t in token_trades if t.roi_pct >= 100)
        rockets_x5  = sum(1 for t in token_trades if t.roi_pct >= 400)
        rockets_x10 = sum(1 for t in token_trades if t.roi_pct >= 900)

        total_trades = sum(t.buy_count + t.sell_count for t in token_trades)

        return WalletStats(
            address=address,
            sol_balance=sol_balance,
            usd_balance=sol_balance * sol_price_usd,
            winrate=winrate,
            roi=roi_overall,
            median_roi=median_roi,
            avg_roi=avg_roi,
            pnl_sol=pnl_sol,
            spent_sol=spent_sol,
            earned_sol=earned_sol,
            smtb_spl=smtb_spl,
            tokens_count=len(token_trades),
            avg_buys=avg_buys,
            avg_sells=avg_sells,
            avg_sell_pct=avg_sell_pct,
            median_sell_pct=median_sell_pct,
            avg_spl_holdings=avg_spl_holdings,
            holding_positions=holding_positions,
            median_trade_duration=median_td,
            avg_trade_duration=avg_td,
            avg_duration_to_first_sell=avg_ttfs,
            last_swap_ts=last_swap_ts,
            first_swap_ts=first_swap_ts,
            avg_swap_fee_sol=avg_fee,
            median_avg_swap_fee_sol=median_fee,
            multi_trans_swaps_pct=multi_pct,
            rockets_x2=rockets_x2,
            rockets_x5=rockets_x5,
            rockets_x10=rockets_x10,
            trade_providers=list(set(all_dexes)),
            pool_providers=list(set(all_pools)),
            pool_type_counts=pool_type_counts,
            aggregator_counts=aggregator_counts,
            total_trades=total_trades,
            token_trades=sorted(token_trades, key=lambda t: t.pnl_sol, reverse=True),
        )
