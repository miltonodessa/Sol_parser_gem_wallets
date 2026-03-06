"""
scanner.py — discovers active trader wallets by scanning Solana DEX programs.

Strategy
--------
For every DEX program in SCAN_PROGRAMS we fetch recent SWAP transactions via
the Helius Enhanced Transactions API.  Each transaction carries a `feePayer`
field — the wallet that signed and paid for the swap.  We collect unique
fee-payers across all programs to build a set of active trader wallets.

This lets main.py work with no input file at all: it discovers wallets straight
from the blockchain and then feeds them to the existing analysis pipeline.
"""

import time
from typing import Dict, List, Optional, Set

import config as cfg

# ── DEX / protocol programs to scan ──────────────────────────────────────────
# Ordered roughly by trading volume so we discover the most active wallets first.

SCAN_PROGRAMS: Dict[str, str] = {
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "Pump.fun",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "Pump.fun AMM",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3sFt2As": "Orca Whirlpool",
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter v6",
    "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qffwaryk": "Moonshot",
    "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP": "Orca v2",
}

# Friendly alias → program ID (for --scan-programs CLI argument)
PROGRAM_ALIASES: Dict[str, str] = {
    "pump":     "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
    "pumpamm":  "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
    "raydium":  "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "raydiumclmm": "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
    "orca":     "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3sFt2As",
    "orcav2":   "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP",
    "jupiter":  "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    "moonshot": "MoonCVVNZFSYkqNXP6bxHLPL6QQJiMagDL3qffwaryk",
}


class BlockchainScanner:
    """Discovers unique trader wallets by scraping recent DEX swap transactions."""

    _PAGE_SLEEP = 0.15   # seconds between Helius API pages

    def __init__(self, fetcher) -> None:
        # Re-use the WalletFetcher session / helper methods.
        self._fetcher = fetcher

    # ── public ────────────────────────────────────────────────────────────────

    def discover_wallets(
        self,
        programs: Optional[List[str]] = None,
        max_wallets: int = 1000,
        scan_txs_per_program: int = 2000,
        period_days: Optional[int] = None,
        dedupe_existing: Optional[Set[str]] = None,
        verbose: bool = True,
    ) -> List[str]:
        """
        Scan DEX program accounts and return a de-duplicated list of wallet
        addresses that made at least one SWAP within *period_days*.

        Parameters
        ----------
        programs            : list of program IDs to scan; None → all defaults
        max_wallets         : stop after collecting this many unique wallets
        scan_txs_per_program: max transactions to fetch per program
        period_days         : ignore transactions older than this; None → no limit
        dedupe_existing     : set of addresses already processed — skip them
        verbose             : print progress lines
        """
        targets = programs or list(SCAN_PROGRAMS.keys())
        seen_wallets: Set[str] = set(dedupe_existing or [])
        discovered: List[str] = []
        cutoff: Optional[float] = (
            time.time() - period_days * 86_400 if period_days else None
        )

        for prog_id in targets:
            label = SCAN_PROGRAMS.get(prog_id, prog_id[:12] + "…")
            if verbose:
                print(f"  Scanning {label} ({prog_id[:12]}…)", flush=True)

            remaining = max_wallets - len(discovered)   # always > 0 here
            prog_wallets = self._scan_program(
                prog_id,
                max_txs=scan_txs_per_program,
                cutoff=cutoff,
                seen=seen_wallets,
                cap=remaining,
            )

            discovered.extend(prog_wallets)
            seen_wallets.update(prog_wallets)

            if verbose:
                print(
                    f"    +{len(prog_wallets)} new wallets  "
                    f"(total so far: {len(discovered)})"
                )

            if len(discovered) >= max_wallets:
                if verbose:
                    limit_str = (
                        f"{max_wallets:,}" if max_wallets < 10_000_000 else "unlimited"
                    )
                    print(f"  Reached scan-limit ({limit_str}), stopping scan.")
                break

        return discovered

    # ── private ───────────────────────────────────────────────────────────────

    def _scan_program(
        self,
        program_id: str,
        max_txs: int,
        cutoff: Optional[float],
        seen: Set[str],
        cap: int,
    ) -> List[str]:
        """
        Page through Helius SWAP transactions for *program_id* and collect
        unique fee-payer addresses not already in *seen*.
        """
        wallets: List[str] = []
        before_sig: Optional[str] = None
        fetched = 0

        while fetched < max_txs and len(wallets) < cap:
            params: dict = {
                "api-key": cfg.HELIUS_API_KEY,
                "type": "SWAP",
                "limit": 100,
            }
            if before_sig:
                params["before"] = before_sig

            url = f"{cfg.HELIUS_API_URL}/addresses/{program_id}/transactions"
            batch = self._fetcher._get(url, params=params)

            if batch is None:
                # API error (network / auth) — stop paging this program
                break
            if not batch:
                # empty page — no more transactions
                break

            for tx in batch:
                # honour time-window
                ts = tx.get("timestamp", 0)
                if cutoff and ts < cutoff:
                    return wallets   # transactions are newest-first → done

                payer = tx.get("feePayer") or ""
                if not payer or len(payer) < 32:
                    continue
                if payer in seen:
                    continue

                seen.add(payer)
                wallets.append(payer)

                if len(wallets) >= cap:
                    return wallets

            fetched += len(batch)
            if len(batch) < 100:
                break

            before_sig = batch[-1]["signature"]
            time.sleep(self._PAGE_SLEEP)

        return wallets
