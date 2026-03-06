"""
fetcher.py — fetches Solana wallet data via Helius Enhanced Transactions API
and Solana JSON-RPC.
"""

import time
import sys
from typing import List, Dict, Optional

import requests

import config as cfg


class WalletFetcher:
    """Fetches raw on-chain data for a Solana wallet."""

    # Helius free tier: ~10 req/s — small sleep between pages is enough
    _PAGE_SLEEP = 0.12
    _RETRY_SLEEPS = [2, 4, 8, 16]

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ── helpers ──────────────────────────────────────────────────────────────

    def _get(self, url: str, params: dict = None, retries: int = 4) -> Optional[dict]:
        for attempt, sleep in enumerate([0] + self._RETRY_SLEEPS[:retries]):
            if sleep:
                time.sleep(sleep)
            try:
                r = self._session.get(url, params=params, timeout=30)
                if r.status_code == 429:          # rate-limited
                    time.sleep(sleep or 2)
                    continue
                if r.status_code == 200:
                    return r.json()
            except requests.RequestException as exc:
                if attempt == retries:
                    print(f"  [fetcher] GET {url} failed: {exc}", file=sys.stderr)
        return None

    def _post_rpc(self, method: str, params: list, retries: int = 3) -> Optional[dict]:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        for attempt, sleep in enumerate([0] + self._RETRY_SLEEPS[:retries]):
            if sleep:
                time.sleep(sleep)
            try:
                r = self._session.post(cfg.RPC_URL, json=payload, timeout=15)
                if r.status_code == 200:
                    data = r.json()
                    if "result" in data:
                        return data["result"]
            except requests.RequestException as exc:
                if attempt == retries:
                    print(f"  [fetcher] RPC {method} failed: {exc}", file=sys.stderr)
        return None

    # ── public API ────────────────────────────────────────────────────────────

    def get_sol_balance(self, address: str) -> float:
        """Return SOL balance (float) for the wallet."""
        result = self._post_rpc("getBalance", [address])
        if result is None:
            return 0.0
        return result.get("value", 0) / cfg.LAMPORTS_PER_SOL

    def get_sol_price_usd(self) -> float:
        """Return current SOL price in USD (Jupiter price API)."""
        data = self._get(
            "https://price.jup.ag/v4/price",
            params={"ids": cfg.WSOL_MINT},
        )
        try:
            return float(data["data"][cfg.WSOL_MINT]["price"])
        except (TypeError, KeyError):
            return 150.0   # sensible fallback

    def get_swap_transactions(
        self,
        address: str,
        period_days: Optional[int] = None,
    ) -> List[Dict]:
        """
        Return a list of parsed Helius Enhanced Transactions of type SWAP
        for *address* within *period_days* (None → all time).

        Each item is a raw Helius transaction dict.
        """
        cutoff: Optional[float] = (
            time.time() - period_days * 86400 if period_days else None
        )

        transactions: List[Dict] = []
        before_sig: Optional[str] = None

        while True:
            params: Dict = {
                "api-key": cfg.HELIUS_API_KEY,
                "type": "SWAP",
                "limit": 100,
            }
            if before_sig:
                params["before"] = before_sig

            url = f"{cfg.HELIUS_API_URL}/addresses/{address}/transactions"
            batch = self._get(url, params=params)

            if not batch:
                break

            for tx in batch:
                ts = tx.get("timestamp", 0)
                if cutoff and ts < cutoff:
                    return transactions
                transactions.append(tx)

            if len(batch) < 100:
                break

            before_sig = batch[-1]["signature"]
            time.sleep(self._PAGE_SLEEP)

        return transactions

    def get_token_accounts(self, address: str) -> List[Dict]:
        """
        Return SPL token accounts for a wallet.
        Each item: {"mint": str, "amount": float}
        """
        result = self._post_rpc(
            "getTokenAccountsByOwner",
            [
                address,
                {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
                {"encoding": "jsonParsed"},
            ],
        )
        accounts = []
        if not result:
            return accounts
        for item in result.get("value", []):
            info = (
                item.get("account", {})
                .get("data", {})
                .get("parsed", {})
                .get("info", {})
            )
            mint = info.get("mint")
            raw_amount = info.get("tokenAmount", {}).get("uiAmount", 0) or 0
            if mint and raw_amount > 0:
                accounts.append({"mint": mint, "amount": raw_amount})
        return accounts
