#!/usr/bin/env python3
"""
main.py — Froggy Scanner: Solana Wallet Parser
=================================================
Fetch, analyse and filter Solana wallets using the Helius Enhanced
Transactions API, then export to Excel / TXT with GMGN token links.

Quick start
-----------
1. Copy .env.example → .env and fill in your HELIUS_API_KEY
   (free key at https://www.helius.dev/)
2. Create a file with one wallet address per line, e.g. wallets.txt
3. Run:
       python main.py -i wallets.txt

Full example with filters:
       python main.py -i wallets.txt \\
         --period-days 30 \\
         --min-sol-invest 0.1 \\
         --winrate-min 50 \\
         --pnl-min 1.0 \\
         --roi-min 50 \\
         --total-trades-min 10 \\
         --last-trade-max 7d \\
         --rockets-x2 \\
         --format Excel+TXT \\
         --extended
"""

import argparse
import sys

from config import HELIUS_API_KEY, FilterConfig
from src.fetcher import WalletFetcher
from src.analyzer import WalletAnalyzer
from src.filters import FilterEngine
from src.exporter import DataExporter


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="froggy-scanner",
        description="Froggy Scanner — Solana Wallet Parser",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── Input / Output ──────────────────────────────────────────────────────
    p.add_argument("-i", "--input", required=True,
                   help="Input file — one Solana wallet address per line "
                        "(lines starting with # are ignored)")
    p.add_argument("-o", "--output-dir", default="output",
                   help="Directory for output files (default: output/)")
    p.add_argument("--format", default="Excel",
                   choices=["Excel", "Excel+TXT", "TXT"],
                   help="Output format (default: Excel)")
    p.add_argument("--extended", action="store_true",
                   help="Extended mode: add a Token Trades sheet with every "
                        "individual token position and GMGN links")
    p.add_argument("--no-filters", action="store_true",
                   help="Disable all filters — export every wallet analysed")

    # ── API ─────────────────────────────────────────────────────────────────
    p.add_argument("--api-key",
                   help="Helius API key (overrides HELIUS_API_KEY env var)")

    # ── Token SOL Invest ────────────────────────────────────────────────────
    p.add_argument("--min-sol-invest", type=float, default=None,
                   metavar="{5|1|0.5|0.1|0.05}",
                   help="Minimum SOL invested per token to include in analysis "
                        "(default: All — no minimum)")

    # ── Period ──────────────────────────────────────────────────────────────
    period_grp = p.add_mutually_exclusive_group()
    period_grp.add_argument("--period-days", type=int, default=45,
                             choices=[1, 7, 14, 30, 45, 90],
                             metavar="{1|7|14|30|45|90}",
                             help="Analysis window in days (default: 45)")
    period_grp.add_argument("--period-max", action="store_true",
                             help="Use full history (Max period)")

    # ── Performance ─────────────────────────────────────────────────────────
    p.add_argument("--winrate-min", type=float, default=0.0,
                   metavar="PCT", help="Min winrate %% (0-100, default 0)")
    p.add_argument("--winrate-max", type=float, default=100.0,
                   metavar="PCT", help="Max winrate %% (0-100, default 100)")
    p.add_argument("--pnl-min", type=float, default=None,
                   metavar="SOL", help="Min PNL in SOL")
    p.add_argument("--pnl-max", type=float, default=None,
                   metavar="SOL", help="Max PNL in SOL")

    # ── ROI Settings ────────────────────────────────────────────────────────
    p.add_argument("--roi-min",        type=float, default=None, metavar="PCT")
    p.add_argument("--roi-max",        type=float, default=None, metavar="PCT")
    p.add_argument("--median-roi-min", type=float, default=None, metavar="PCT")
    p.add_argument("--median-roi-max", type=float, default=None, metavar="PCT")
    p.add_argument("--avg-roi-min",    type=float, default=None, metavar="PCT")
    p.add_argument("--avg-roi-max",    type=float, default=None, metavar="PCT")

    # ── Balances ────────────────────────────────────────────────────────────
    p.add_argument("--sol-min", type=float, default=None,
                   metavar="SOL", help="Min SOL balance")
    p.add_argument("--sol-max", type=float, default=None,
                   metavar="SOL", help="Max SOL balance")
    p.add_argument("--usd-min", type=float, default=None,
                   metavar="USD", help="Min USD balance")
    p.add_argument("--usd-max", type=float, default=None,
                   metavar="USD", help="Max USD balance")

    # ── Trading ─────────────────────────────────────────────────────────────
    p.add_argument("--trade-providers", nargs="*", default=[],
                   metavar="NAME",
                   help="Filter by DEX (e.g. Raydium Orca Jupiter)")
    p.add_argument("--pool-providers", nargs="*", default=[],
                   metavar="NAME",
                   help="Filter by pool providers")
    p.add_argument("--pool-type",
                   help="Filter by pool type name")
    p.add_argument("--aggregators", nargs="*", default=[],
                   metavar="NAME",
                   help="Filter by aggregator (e.g. Jupiter)")

    # ── Last / First Trade ──────────────────────────────────────────────────
    p.add_argument("--last-trade-min",
                   metavar="DUR",
                   help="Min time since last trade (e.g. 1h, 30m, 1d12h)")
    p.add_argument("--last-trade-max",
                   metavar="DUR",
                   help="Max time since last trade (e.g. 7d)")
    p.add_argument("--first-trade-min",
                   metavar="DUR",
                   help="Min time since first trade")
    p.add_argument("--first-trade-max",
                   metavar="DUR",
                   help="Max time since first trade")

    # ── Total Trades ────────────────────────────────────────────────────────
    p.add_argument("--total-trades-min", type=int, default=None,
                   metavar="N", help="Min total swap count")
    p.add_argument("--total-trades-max", type=int, default=None,
                   metavar="N", help="Max total swap count")

    # ── Rockets count ────────────────────────────────────────────────────────
    p.add_argument("--rockets-x2",  action="store_true",
                   help="Wallet must have ≥1 token with ≥2× ROI")
    p.add_argument("--rockets-x5",  action="store_true",
                   help="Wallet must have ≥1 token with ≥5× ROI")
    p.add_argument("--rockets-x10", action="store_true",
                   help="Wallet must have ≥1 token with ≥10× ROI")

    # ── Trade Duration ───────────────────────────────────────────────────────
    p.add_argument("--trade-dur-min", type=int, default=None,
                   metavar="SEC", help="Min avg trade duration in seconds")
    p.add_argument("--trade-dur-max", type=int, default=None,
                   metavar="SEC", help="Max avg trade duration in seconds")

    return p


# ── helpers ───────────────────────────────────────────────────────────────────

def load_wallets(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]


def _banner() -> None:
    print()
    print("  ╔══════════════════════════════════════════╗")
    print("  ║   🐸 Froggy Scanner — Solana Wallet Parser  ║")
    print("  ╚══════════════════════════════════════════╝")
    print()


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _banner()
    args = build_parser().parse_args()

    # ── API key ─────────────────────────────────────────────────────────────
    api_key = args.api_key or HELIUS_API_KEY
    if not api_key:
        print("[ERROR] No Helius API key found.")
        print("  Set HELIUS_API_KEY in your .env file or pass --api-key.")
        print("  Free key: https://www.helius.dev/")
        sys.exit(1)

    import config as cfg
    cfg.HELIUS_API_KEY = api_key
    cfg.RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={api_key}"

    # ── Load wallet list ────────────────────────────────────────────────────
    try:
        wallet_addresses = load_wallets(args.input)
    except FileNotFoundError:
        print(f"[ERROR] Input file not found: {args.input}")
        sys.exit(1)

    if not wallet_addresses:
        print("[ERROR] No wallet addresses in input file.")
        sys.exit(1)

    print(f"  Loaded {len(wallet_addresses)} wallet address(es) from {args.input}")

    # ── Build FilterConfig ──────────────────────────────────────────────────
    filter_cfg = FilterConfig(
        min_sol_invest   = args.min_sol_invest,
        period_days      = None if args.period_max else args.period_days,
        winrate_min      = args.winrate_min,
        winrate_max      = args.winrate_max,
        pnl_sol_min      = args.pnl_min,
        pnl_sol_max      = args.pnl_max,
        roi_min          = args.roi_min,
        roi_max          = args.roi_max,
        median_roi_min   = args.median_roi_min,
        median_roi_max   = args.median_roi_max,
        avg_roi_min      = args.avg_roi_min,
        avg_roi_max      = args.avg_roi_max,
        total_sol_min    = args.sol_min,
        total_sol_max    = args.sol_max,
        total_usd_min    = args.usd_min,
        total_usd_max    = args.usd_max,
        trade_providers  = args.trade_providers,
        pool_providers   = args.pool_providers,
        pool_type        = args.pool_type,
        aggregators      = args.aggregators,
        last_trade_min   = args.last_trade_min,
        last_trade_max   = args.last_trade_max,
        first_trade_min  = args.first_trade_min,
        first_trade_max  = args.first_trade_max,
        total_trades_min = args.total_trades_min,
        total_trades_max = args.total_trades_max,
        rockets_x2       = args.rockets_x2,
        rockets_x5       = args.rockets_x5,
        rockets_x10      = args.rockets_x10,
        trade_duration_min = args.trade_dur_min,
        trade_duration_max = args.trade_dur_max,
        active_filters   = not args.no_filters,
    )

    period_label = f"{filter_cfg.period_days}d" if filter_cfg.period_days else "Max"
    print(f"  Period: {period_label}  |  Min SOL invest: "
          f"{filter_cfg.min_sol_invest or 'All'}  |  "
          f"Filters: {'ON' if filter_cfg.active_filters else 'OFF'}")

    # ── Initialise components ───────────────────────────────────────────────
    fetcher  = WalletFetcher()
    analyzer = WalletAnalyzer()
    engine   = FilterEngine()
    exporter = DataExporter()

    # ── Fetch SOL price once ────────────────────────────────────────────────
    print("\n  Fetching SOL/USD price …")
    sol_price_usd = fetcher.get_sol_price_usd()
    print(f"  SOL price: ${sol_price_usd:,.2f}")

    # ── Process wallets ─────────────────────────────────────────────────────
    all_stats = []
    total = len(wallet_addresses)

    print(f"\n  Processing {total} wallet(s) …\n")

    for idx, address in enumerate(wallet_addresses, 1):
        # basic address validation
        if not (32 <= len(address) <= 44):
            print(f"  [{idx:>4}/{total}] SKIP (invalid address) — {address}")
            continue

        print(f"  [{idx:>4}/{total}] {address[:20]}…", end=" ", flush=True)

        try:
            sol_bal = fetcher.get_sol_balance(address)
            raw_txs = fetcher.get_swap_transactions(address, filter_cfg.period_days)

            if not raw_txs:
                print(f"| {sol_bal:.3f} SOL | 0 swaps — skipped")
                continue

            stats = analyzer.analyze_wallet(
                address,
                raw_txs,
                sol_bal,
                sol_price_usd,
                min_sol_invest=filter_cfg.min_sol_invest,
            )
            all_stats.append(stats)

            print(
                f"| {stats.sol_balance:.3f} SOL"
                f"  WR:{stats.winrate * 100:.0f}%"
                f"  PNL:{stats.pnl_sol:+.3f} SOL"
                f"  ROI:{stats.roi:.1f}%"
                f"  Tokens:{stats.tokens_count}"
                f"  x2:{stats.rockets_x2}/x5:{stats.rockets_x5}/x10:{stats.rockets_x10}"
            )

        except KeyboardInterrupt:
            print("\n\n  Interrupted — saving data collected so far …")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"| ERROR: {exc}")
            continue

    # ── Apply filters ───────────────────────────────────────────────────────
    print(f"\n  Analysed : {len(all_stats)} wallets")

    filtered = engine.apply(all_stats, filter_cfg)
    print(f"  Matched  : {len(filtered)} wallets after filters")

    if not filtered:
        print("  No wallets match the specified criteria — nothing to export.")
        sys.exit(0)

    # Sort by PNL descending
    filtered.sort(key=lambda w: w.pnl_sol, reverse=True)

    # ── Export ───────────────────────────────────────────────────────────────
    print(f"\n  Exporting {len(filtered)} wallet(s) → {args.format} …")
    exporter.export(filtered, filter_cfg, args.format, args.extended, args.output_dir)
    print("\n  Done! 🐸\n")


if __name__ == "__main__":
    main()
