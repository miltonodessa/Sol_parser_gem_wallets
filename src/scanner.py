"""
scanner.py — discovers active trader wallets by scanning Solana DEX programs.

Strategy
--------
For every DEX program in SCAN_PROGRAMS we:
  1. Call getSignaturesForAddress (standard Solana RPC) to list recent tx sigs.
     Each entry carries slot + blockTime — no full tx fetch needed yet.
  2. Filter entries by blockTime (time window) and group them by slot number.
  3. For each unique slot, call getBlock once (transactionDetails="accounts").
     One block can cover hundreds of signatures from the same slot — far more
     efficient than one getTransaction call per signature.
  4. Match the block's transaction signatures against our target set and extract
     accountKeys[0] (feePayer — always the first key by Solana protocol).

Why getBlock instead of getTransaction × N:
  Pump.fun can have 300-1 000+ transactions per slot. Fetching 1 000 sigs may
  require only 5-20 getBlock calls instead of 1 000 getTransaction calls.
  getBlock + transactionDetails=accounts is also much lighter than jsonParsed.

No Enhanced Transactions API (api.helius.xyz) is used; only the standard
Solana JSON-RPC endpoint (mainnet.helius-rpc.com) which is free for all plans.
"""

import time
from collections import defaultdict
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
        Discover fee-payer wallets for *program_id*.

        Uses getSignaturesForAddress → group by slot → getBlock per slot.
        One block call covers all signatures in that slot (often hundreds),
        making this far more efficient than one getTransaction per signature.
        """
        wallets: List[str] = []
        before_sig: Optional[str] = None
        fetched = 0

        while fetched < max_txs and len(wallets) < cap:

            # ── 1. get a page of recent signatures ────────────────────────────
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
                break   # no more history

            # ── 2. filter by time window and group by slot ────────────────────
            # slot → set of target signatures in that slot
            slot_sigs: Dict[int, set] = defaultdict(set)
            reached_cutoff = False

            for entry in sig_page:
                if entry.get("err"):       # failed on-chain tx — skip
                    continue
                block_time = entry.get("blockTime") or 0
                if cutoff and block_time and block_time < cutoff:
                    reached_cutoff = True
                    break
                slot = entry.get("slot")
                if slot is not None:
                    slot_sigs[slot].add(entry["signature"])

            # ── 3. one getBlock call per unique slot ──────────────────────────
            for slot, target_sigs in slot_sigs.items():
                block = self._fetcher._post_rpc(
                    "getBlock",
                    [
                        slot,
                        {
                            "encoding": "jsonParsed",
                            "maxSupportedTransactionVersion": 0,
                            "transactionDetails": "accounts",   # minimal payload
                            "rewards": False,
                        },
                    ],
                )
                time.sleep(self._PAGE_SLEEP)

                if not isinstance(block, dict):
                    continue

                for tx in block.get("transactions", []):
                    try:
                        tx_data = tx["transaction"]
                        sig = tx_data["signatures"][0]
                        if sig not in target_sigs:
                            continue   # not one of ours
                        # accountKeys[0] is always feePayer (Solana protocol)
                        first = tx_data["accountKeys"][0]
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
                break   # last page or passed the time cutoff

            before_sig = sig_page[-1]["signature"]

        return wallets
