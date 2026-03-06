#!/usr/bin/env python3
"""
main.py — Froggy Scanner: Solana Wallet Parser
=================================================
Scans the Solana blockchain for active DEX traders, analyses every discovered
wallet, applies filters, and exports results to Excel / TXT.

No input file is needed.  The scanner discovers wallets by reading recent
SWAP transactions directly from DEX program accounts on-chain.

Quick start
-----------
  # copy .env.example → .env  and fill in HELIUS_API_KEY
  python main.py

  # scan specific DEX only, top-5000 wallets, last 30 days
  python main.py --scan-programs pump raydium --scan-limit 5000 --period-days 30

  # add filters and export
  python main.py \\
    --period-days 30           \\
    --min-sol-invest 0.1       \\
    --winrate-min 50           \\
    --pnl-min 1.0              \\
    --roi-min 50               \\
    --total-trades-min 10      \\
    --last-trade-max 7d        \\
    --rockets-x2               \\
    --format Excel+TXT         \\
    --extended

  # also accept a list of known addresses (appended to scanned set)
  python main.py -i extra_wallets.txt --scan-limit 500
"""

import argparse
import sys
from typing import List, Optional, Set

from config import HELIUS_API_KEY, FilterConfig
from src.fetcher import WalletFetcher
from src.analyzer import WalletAnalyzer
from src.filters import FilterEngine
from src.exporter import DataExporter
from src.scanner import BlockchainScanner, SCAN_PROGRAMS, PROGRAM_ALIASES


# ── CLI ───────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="froggy-scanner",
        description="Froggy Scanner — scans Solana DEX activity, analyses wallets, exports results",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    # ── Blockchain scan ─────────────────────────────────────────────────────
    scan = p.add_argument_group("Blockchain scan")
    scan.add_argument("--scan-limit", type=int, default=None,
                      metavar="N",
                      help="Max unique wallets to discover from the blockchain. "
                           "If not set, you will be asked interactively.")
    scan.add_argument("--scan-depth", type=int, default=2000,
                      metavar="N",
                      help="Max transactions to read per DEX program "
                           "(default: 2000)")
    scan.add_argument("--scan-programs", nargs="+", default=None,
                      metavar="NAME",
                      help="DEX programs to scan.  Use aliases: "
                           + ", ".join(sorted(PROGRAM_ALIASES)) +
                           ".  Default: all.")

    # ── Optional extra wallet file ───────────────────────────────────────────
    p.add_argument("-i", "--input", default=None,
                   help="Optional extra file with wallet addresses "
                        "(one per line, # = comment).  "
                        "These are appended to wallets found by the scanner.")

    # ── Output ──────────────────────────────────────────────────────────────
    p.add_argument("-o", "--output-dir", default="output",
                   help="Directory for output files (default: output/)")
    p.add_argument("--format", default="Excel",
                   choices=["Excel", "Excel+TXT", "TXT"],
                   help="Output format (default: Excel)")
    p.add_argument("--extended", action="store_true",
                   help="Add a Token Trades sheet with GMGN links per token")
    p.add_argument("--no-filters", action="store_true",
                   help="Disable all filters — export every wallet analysed")

    # ── API ─────────────────────────────────────────────────────────────────
    p.add_argument("--api-key",
                   help="Helius API key (overrides HELIUS_API_KEY env var)")

    # ── Token SOL Invest ────────────────────────────────────────────────────
    p.add_argument("--min-sol-invest", type=float, default=None,
                   metavar="{5|1|0.5|0.1|0.05}",
                   help="Min SOL invested per token (default: All)")

    # ── Period ──────────────────────────────────────────────────────────────
    period_grp = p.add_mutually_exclusive_group()
    period_grp.add_argument("--period-days", type=int, default=45,
                             choices=[1, 7, 14, 30, 45, 90],
                             metavar="{1|7|14|30|45|90}",
                             help="Analysis window in days (default: 45)")
    period_grp.add_argument("--period-max", action="store_true",
                             help="Use full on-chain history (no time limit)")

    # ── Performance ─────────────────────────────────────────────────────────
    p.add_argument("--winrate-min",  type=float, default=0.0,   metavar="PCT")
    p.add_argument("--winrate-max",  type=float, default=100.0, metavar="PCT")
    p.add_argument("--pnl-min",      type=float, default=None,  metavar="SOL")
    p.add_argument("--pnl-max",      type=float, default=None,  metavar="SOL")

    # ── ROI Settings ────────────────────────────────────────────────────────
    p.add_argument("--roi-min",        type=float, default=None, metavar="PCT")
    p.add_argument("--roi-max",        type=float, default=None, metavar="PCT")
    p.add_argument("--median-roi-min", type=float, default=None, metavar="PCT")
    p.add_argument("--median-roi-max", type=float, default=None, metavar="PCT")
    p.add_argument("--avg-roi-min",    type=float, default=None, metavar="PCT")
    p.add_argument("--avg-roi-max",    type=float, default=None, metavar="PCT")

    # ── Balances ────────────────────────────────────────────────────────────
    p.add_argument("--sol-min", type=float, default=None, metavar="SOL")
    p.add_argument("--sol-max", type=float, default=None, metavar="SOL")
    p.add_argument("--usd-min", type=float, default=None, metavar="USD")
    p.add_argument("--usd-max", type=float, default=None, metavar="USD")

    # ── Trading ─────────────────────────────────────────────────────────────
    p.add_argument("--trade-providers", nargs="*", default=[], metavar="NAME")
    p.add_argument("--pool-providers",  nargs="*", default=[], metavar="NAME")
    p.add_argument("--pool-type",       default=None)
    p.add_argument("--aggregators",     nargs="*", default=[], metavar="NAME")

    # ── Last / First Trade ──────────────────────────────────────────────────
    p.add_argument("--last-trade-min",  metavar="DUR")
    p.add_argument("--last-trade-max",  metavar="DUR",
                   help="e.g. 7d, 12h, 30m")
    p.add_argument("--first-trade-min", metavar="DUR")
    p.add_argument("--first-trade-max", metavar="DUR")

    # ── Total Trades ────────────────────────────────────────────────────────
    p.add_argument("--total-trades-min", type=int, default=None, metavar="N")
    p.add_argument("--total-trades-max", type=int, default=None, metavar="N")

    # ── Rockets ─────────────────────────────────────────────────────────────
    p.add_argument("--rockets-x2",  action="store_true",
                   help="Must have ≥1 token with ≥2× ROI")
    p.add_argument("--rockets-x5",  action="store_true",
                   help="Must have ≥1 token with ≥5× ROI")
    p.add_argument("--rockets-x10", action="store_true",
                   help="Must have ≥1 token with ≥10× ROI")

    # ── Trade Duration ───────────────────────────────────────────────────────
    p.add_argument("--trade-dur-min", type=int, default=None, metavar="SEC")
    p.add_argument("--trade-dur-max", type=int, default=None, metavar="SEC")

    return p


# ── helpers ───────────────────────────────────────────────────────────────────

def _resolve_programs(names: Optional[List[str]]) -> Optional[List[str]]:
    """Convert alias names (e.g. 'pump', 'raydium') to program IDs."""
    if not names:
        return None   # use all defaults
    resolved = []
    for name in names:
        key = name.lower()
        if key in PROGRAM_ALIASES:
            resolved.append(PROGRAM_ALIASES[key])
        elif name in SCAN_PROGRAMS:
            resolved.append(name)   # already a program ID
        else:
            print(f"  [WARN] Unknown program alias '{name}' — skipped.")
    return resolved or None


def _load_file_wallets(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        return [
            line.strip()
            for line in f
            if line.strip() and not line.startswith("#")
        ]


def _ask_scan_limit() -> int:
    """Prompt the user to enter how many wallets to scan. Validates input."""
    print("  How many wallets do you want to scan?")
    print("  (Enter a number, e.g. 500 / 5000 / 50000 — or 0 for unlimited)\n")
    while True:
        try:
            raw = input("  Wallets to scan: ").strip()
            val = int(raw)
            if val < 0:
                raise ValueError
            return val if val > 0 else 10_000_000   # 0 → unlimited
        except (ValueError, EOFError):
            print("  Please enter a valid positive integer (or 0 for unlimited).")


def _banner() -> None:
    print()
    print("  ╔══════════════════════════════════════════════╗")
    print("  ║   🐸  Froggy Scanner — Solana Wallet Parser   ║")
    print("  ╚══════════════════════════════════════════════╝")
    print()


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _banner()
    args = build_parser().parse_args()

    # ── API key ─────────────────────────────────────────────────────────────
    api_key = args.api_key or HELIUS_API_KEY
    if not api_key:
        print("[ERROR] Helius API key not found.")
        print("  → Copy .env.example to .env and set HELIUS_API_KEY")
        print("  → Or pass --api-key YOUR_KEY")
        print("  → Free key: https://www.helius.dev/")
        sys.exit(1)

    import config as cfg
    cfg.HELIUS_API_KEY = api_key
    cfg.RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={api_key}"

    # ── FilterConfig ─────────────────────────────────────────────────────────
    period_days = None if args.period_max else args.period_days
    filter_cfg = FilterConfig(
        min_sol_invest   = args.min_sol_invest,
        period_days      = period_days,
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

    # ── Scan limit (interactive if not passed via CLI) ───────────────────────
    scan_limit: int = args.scan_limit if args.scan_limit is not None else _ask_scan_limit()
    print()

    period_label = f"{filter_cfg.period_days}d" if filter_cfg.period_days else "Max"
    limit_label  = f"{scan_limit:,}" if scan_limit < 10_000_000 else "Unlimited"
    print(f"  Period     : {period_label}")
    print(f"  Scan limit : {limit_label} wallets")
    print(f"  Scan depth : {args.scan_depth} txs/program")
    print(f"  Filters    : {'ON' if filter_cfg.active_filters else 'OFF'}")
    print()

    # ── Initialise components ────────────────────────────────────────────────
    fetcher  = WalletFetcher()
    scanner  = BlockchainScanner(fetcher)
    analyzer = WalletAnalyzer()
    engine   = FilterEngine()
    exporter = DataExporter()

    # ── SOL price ────────────────────────────────────────────────────────────
    print("  Fetching SOL/USD price …", flush=True)
    sol_price_usd = fetcher.get_sol_price_usd()
    print(f"  SOL price  : ${sol_price_usd:,.2f}\n")

    # ── Discover wallets from blockchain ─────────────────────────────────────
    programs = _resolve_programs(args.scan_programs)
    program_labels = (
        [SCAN_PROGRAMS.get(p, p) for p in programs]
        if programs
        else list(SCAN_PROGRAMS.values())
    )
    print(f"  Scanning DEX programs: {', '.join(program_labels)}")
    print()

    wallet_addresses: List[str] = scanner.discover_wallets(
        programs=programs,
        max_wallets=scan_limit,
        scan_txs_per_program=args.scan_depth,
        period_days=filter_cfg.period_days,
        verbose=True,
    )

    # ── Merge optional input file ─────────────────────────────────────────────
    if args.input:
        try:
            extra = _load_file_wallets(args.input)
        except FileNotFoundError:
            print(f"  [WARN] Input file not found: {args.input} — ignored")
            extra = []

        existing: Set[str] = set(wallet_addresses)
        added = [w for w in extra if w not in existing]
        wallet_addresses.extend(added)
        if added:
            print(f"\n  Added {len(added)} wallet(s) from {args.input}")

    total = len(wallet_addresses)
    if total == 0:
        print("\n  No wallets discovered — check your API key or scan parameters.")
        sys.exit(1)

    print(f"\n  Discovered {total} unique wallet(s). Starting analysis …\n")

    # ── Analyse each wallet ───────────────────────────────────────────────────
    all_stats = []

    for idx, address in enumerate(wallet_addresses, 1):
        if not (32 <= len(address) <= 44):
            print(f"  [{idx:>5}/{total}] SKIP (invalid) — {address}")
            continue

        print(f"  [{idx:>5}/{total}] {address[:20]}…", end=" ", flush=True)

        try:
            sol_bal = fetcher.get_sol_balance(address)
            raw_txs = fetcher.get_swap_transactions(address, filter_cfg.period_days)

            if not raw_txs:
                print(f"| {sol_bal:.3f} SOL | 0 swaps — skip")
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
                f"  PNL:{stats.pnl_sol:+.3f}"
                f"  ROI:{stats.roi:.0f}%"
                f"  Tok:{stats.tokens_count}"
                f"  🚀{stats.rockets_x2}/{stats.rockets_x5}/{stats.rockets_x10}"
            )

        except KeyboardInterrupt:
            print("\n\n  Interrupted — saving data collected so far …")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"| ERROR: {exc}")
            continue

    # ── Apply filters ─────────────────────────────────────────────────────────
    print(f"\n  Analysed : {len(all_stats)} wallets")
    filtered = engine.apply(all_stats, filter_cfg)
    print(f"  Matched  : {len(filtered)} wallets after filters")

    if not filtered:
        print("  No wallets match the specified criteria.")
        sys.exit(0)

    filtered.sort(key=lambda w: w.pnl_sol, reverse=True)

    # ── Export ────────────────────────────────────────────────────────────────
    print(f"\n  Exporting {len(filtered)} wallet(s) → {args.format} …")
    exporter.export(filtered, filter_cfg, args.format, args.extended, args.output_dir)
    print("\n  Done! 🐸\n")


if __name__ == "__main__":
    main()
