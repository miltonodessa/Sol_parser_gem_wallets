"""
scanner.py — discovers active trader wallets by scanning Solana DEX programs.

Strategy
--------
For every DEX program in SCAN_PROGRAMS we:
  1. Call getSignaturesForAddress (standard Solana RPC) to list recent tx sigs.
  2. Filter by blockTime so we stay within the requested time window.
  3. Batch-fetch full transactions (100 per RPC request) via getTransaction.
  4. Extract feePayer (account[0]) — the wallet that signed and paid the fee.

This uses only the standard Solana JSON-RPC endpoint (mainnet.helius-rpc.com)
so no Enhanced Transactions API / api.helius.xyz access is needed.
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

    # ── RPC batch size — 100 is safe for most RPC nodes ─────────────────────
    _TX_BATCH = 100

    def _scan_program(
        self,
        program_id: str,
        max_txs: int,
        cutoff: Optional[float],
        seen: Set[str],
        cap: int,
    ) -> List[str]:
        """
        Discover fee-payer wallets for *program_id* using standard Solana RPC:

          1. getSignaturesForAddress  → page of up to 1 000 recent tx sigs
          2. filter by blockTime       → skip txs outside the time window
          3. batch getTransaction      → extract feePayer from accountKeys[0]
        """
        wallets: List[str] = []
        before_sig: Optional[str] = None
        fetched = 0

        while fetched < max_txs and len(wallets) < cap:

            # ── 1. signatures page ────────────────────────────────────────────
            sig_params: list = [program_id, {"limit": 1000, "commitment": "finalized"}]
            if before_sig:
                sig_params[1]["before"] = before_sig

            sig_page = self._fetcher._post_rpc("getSignaturesForAddress", sig_params)

            if sig_page is None:
                if fetched == 0:
                    print(
                        f"    [WARN] RPC getSignaturesForAddress failed for "
                        f"{program_id[:12]}… — check RPC URL / API key",
                        flush=True,
                    )
                break

            if not sig_page:
                break   # no more signatures

            # ── 2. filter by time window ──────────────────────────────────────
            valid_sigs: List[str] = []
            reached_cutoff = False
            for entry in sig_page:
                if entry.get("err"):          # failed on-chain tx — skip
                    continue
                block_time = entry.get("blockTime") or 0
                if cutoff and block_time and block_time < cutoff:
                    reached_cutoff = True
                    break
                valid_sigs.append(entry["signature"])

            # ── 3. batch-fetch transactions ───────────────────────────────────
            for chunk_start in range(0, len(valid_sigs), self._TX_BATCH):
                chunk = valid_sigs[chunk_start: chunk_start + self._TX_BATCH]

                calls = [
                    {
                        "jsonrpc": "2.0",
                        "id": i,
                        "method": "getTransaction",
                        "params": [
                            sig,
                            {
                                "encoding": "jsonParsed",
                                "maxSupportedTransactionVersion": 0,
                                "commitment": "finalized",
                            },
                        ],
                    }
                    for i, sig in enumerate(chunk)
                ]

                results = self._fetcher._post_rpc_batch(calls)
                time.sleep(self._PAGE_SLEEP)

                for tx in results:
                    if not isinstance(tx, dict):
                        continue
                    try:
                        keys = tx["transaction"]["message"]["accountKeys"]
                        # parsed encoding: list of {"pubkey":…, "signer":…}
                        # legacy encoding: list of plain base58 strings
                        first = keys[0]
                        payer = first["pubkey"] if isinstance(first, dict) else first
                    except (KeyError, IndexError, TypeError):
                        continue

                    if not payer or len(payer) < 32 or payer in seen:
                        continue

                    seen.add(payer)
                    wallets.append(payer)

                    if len(wallets) >= cap:
                        return wallets

            fetched += len(sig_page)

            if len(sig_page) < 1000 or reached_cutoff:
                break   # last page or hit time cutoff

            before_sig = sig_page[-1]["signature"]

        return wallets
