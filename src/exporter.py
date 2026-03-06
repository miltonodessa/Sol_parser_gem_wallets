"""
exporter.py — exports WalletStats to Excel (.xlsx) and/or plain TXT.

Excel sheets produced:
  • Summary        — report metadata + applied-filter summary
  • Wallets        — one row per wallet, columns A-Z match the original schema
  • Token Trades   — one row per token-trade (extended mode only)
                     Column "GMGN Link" is a clickable hyperlink

TXT format:
  Plain human-readable list of wallets + per-token GMGN links.
"""

import os
import time
from datetime import datetime, timezone
from typing import List

import openpyxl
from openpyxl.styles import (
    Alignment,
    Border,
    Font,
    PatternFill,
    Side,
)
from openpyxl.utils import get_column_letter

from config import FilterConfig
from src.analyzer import WalletStats
from src.filters import seconds_to_human


# ── helpers ───────────────────────────────────────────────────────────────────

# Excel serial date epoch: 1900-01-01 (with Lotus 1900 leap-year bug → day 1 = 1)
_EXCEL_EPOCH = datetime(1899, 12, 30, tzinfo=timezone.utc)


def _ts_to_excel_serial(ts: int) -> float:
    """Convert unix timestamp to Excel serial date number."""
    if not ts:
        return 0.0
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    return (dt - _EXCEL_EPOCH).total_seconds() / 86_400


def _ts_to_str(ts: int) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _time_ago(ts: int) -> str:
    if not ts:
        return ""
    diff = int(time.time()) - ts
    return seconds_to_human(diff) + " ago"


# ── colour palette ────────────────────────────────────────────────────────────

_BG_DARK       = "111827"
_BG_ROW_ALT    = "1F2937"
_BG_HEADER     = "0F172A"
_GREEN         = "10B981"
_RED           = "EF4444"
_ACCENT        = "6366F1"
_GOLD          = "F59E0B"
_WHITE         = "F9FAFB"
_GREY          = "9CA3AF"
_LINK          = "38BDF8"


def _hdr_font(size: int = 9, bold: bool = True) -> Font:
    return Font(name="Calibri", size=size, bold=bold, color=_WHITE)


def _cell_font(color: str = _WHITE, bold: bool = False, size: int = 9) -> Font:
    return Font(name="Calibri", size=size, bold=bold, color=color)


def _fill(hex_color: str) -> PatternFill:
    return PatternFill(start_color=hex_color, end_color=hex_color, fill_type="solid")


def _centre() -> Alignment:
    return Alignment(horizontal="center", vertical="center", wrap_text=False)


def _thin_border() -> Border:
    s = Side(style="thin", color="374151")
    return Border(left=s, right=s, top=s, bottom=s)


# ── exporter ──────────────────────────────────────────────────────────────────

class DataExporter:

    def export(
        self,
        wallets: List[WalletStats],
        cfg: FilterConfig,
        output_format: str,   # "Excel" | "Excel+TXT" | "TXT"
        extended_mode: bool,
        output_dir: str = ".",
    ) -> None:
        os.makedirs(output_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if output_format in ("Excel", "Excel+TXT"):
            path = os.path.join(output_dir, f"wallets_report_{stamp}.xlsx")
            self._write_excel(wallets, cfg, extended_mode, path)
            print(f"  Saved Excel → {path}")

        if output_format in ("TXT", "Excel+TXT"):
            path = os.path.join(output_dir, f"wallets_report_{stamp}.txt")
            self._write_txt(wallets, path)
            print(f"  Saved TXT   → {path}")

    # ── Excel ─────────────────────────────────────────────────────────────

    def _write_excel(
        self,
        wallets: List[WalletStats],
        cfg: FilterConfig,
        extended_mode: bool,
        path: str,
    ) -> None:
        wb = openpyxl.Workbook()
        ws_sum = wb.active
        ws_sum.title = "Summary"
        self._sheet_summary(ws_sum, wallets, cfg)

        ws_w = wb.create_sheet(f"Wallets ({len(wallets)})")
        self._sheet_wallets(ws_w, wallets)

        if extended_mode:
            ws_t = wb.create_sheet("Token Trades")
            self._sheet_tokens(ws_t, wallets)

        wb.save(path)

    # ── Summary sheet ──────────────────────────────────────────────────────

    def _sheet_summary(self, ws, wallets: List[WalletStats], cfg: FilterConfig) -> None:
        ws.sheet_view.showGridLines = False
        ws.sheet_properties.tabColor = _ACCENT[2:]

        # title
        ws["A1"] = "Froggy Scanner — Solana Wallet Report"
        ws["A1"].font = Font(name="Calibri", size=16, bold=True, color=_GREEN)
        ws.merge_cells("A1:D1")
        ws.row_dimensions[1].height = 30

        ws["A2"] = f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        ws["A2"].font = _cell_font(_GREY, size=9)
        ws.merge_cells("A2:D2")

        row = 4
        stats = [
            ("Wallets analysed",    len(wallets)),
            ("Profitable wallets",  sum(1 for w in wallets if w.pnl_sol > 0)),
            ("Avg Winrate",         f"{sum(w.winrate for w in wallets) / len(wallets) * 100:.1f}%"
                                    if wallets else "—"),
            ("Total PNL (SOL)",     f"{sum(w.pnl_sol for w in wallets):.4f}"),
            ("Period days",         cfg.period_days if cfg.period_days else "Max"),
            ("Min SOL invest",      cfg.min_sol_invest if cfg.min_sol_invest is not None else "All"),
        ]
        for label, val in stats:
            ws[f"A{row}"] = label
            ws[f"B{row}"] = val
            ws[f"A{row}"].font = _cell_font(_GREY, bold=True)
            ws[f"B{row}"].font = _cell_font(_WHITE, bold=True)
            row += 1

        row += 1
        ws[f"A{row}"] = "Active Filters"
        ws[f"A{row}"].font = Font(name="Calibri", size=11, bold=True, color=_ACCENT)
        row += 1

        applied = []
        if cfg.winrate_min > 0 or cfg.winrate_max < 100:
            applied.append(f"Winrate: {cfg.winrate_min}% – {cfg.winrate_max}%")
        for label, lo, hi in [
            ("PNL SOL",    cfg.pnl_sol_min,     cfg.pnl_sol_max),
            ("ROI %",      cfg.roi_min,          cfg.roi_max),
            ("Median ROI", cfg.median_roi_min,   cfg.median_roi_max),
            ("Avg ROI",    cfg.avg_roi_min,       cfg.avg_roi_max),
            ("SOL bal",    cfg.total_sol_min,    cfg.total_sol_max),
            ("USD bal",    cfg.total_usd_min,    cfg.total_usd_max),
            ("Trades",     cfg.total_trades_min, cfg.total_trades_max),
        ]:
            if lo is not None or hi is not None:
                applied.append(
                    f"{label}: {lo if lo is not None else '—'} → {hi if hi is not None else '—'}"
                )
        if cfg.last_trade_max:
            applied.append(f"Last trade ≤ {cfg.last_trade_max}")
        if cfg.rockets_x2:  applied.append("Rockets x2 ✓")
        if cfg.rockets_x5:  applied.append("Rockets x5 ✓")
        if cfg.rockets_x10: applied.append("Rockets x10 ✓")
        if cfg.trade_providers:
            applied.append(f"Providers: {', '.join(cfg.trade_providers)}")
        if cfg.aggregators:
            applied.append(f"Aggregators: {', '.join(cfg.aggregators)}")

        if not applied:
            applied = ["(none — all wallets shown)"]

        for item in applied:
            ws[f"A{row}"] = item
            ws[f"A{row}"].font = _cell_font(_GREY)
            row += 1

        ws.column_dimensions["A"].width = 32
        ws.column_dimensions["B"].width = 22

    # ── Wallets sheet ──────────────────────────────────────────────────────

    _WALLET_HEADERS = [
        "Wallet",
        "SOL Balance",
        "USD Balance",
        "Winrate",
        "ROI",
        "Median ROI",
        "AVG ROI",
        "PNL SOL",
        "Spent SOL",
        "Earned SOL",
        "SMTB SPL",
        "Tokens",
        "AVG Buys",
        "AVG Sells",
        "AVG Sell %",
        "Median Sell %",
        "AVG SPL Holdings",
        "Holding positions",
        "Median Trade Duration",
        "AVG Trade Duration",
        "Avg Duration to First Sell",
        "Last Swap",
        "First Swap",
        "AVG Swap Fee SOL",
        "Median AVG Swap Fee SOL",
        "Multi Trans Swaps %",
    ]

    # column widths matching the original file
    _WALLET_COL_WIDTHS = [
        50, 16, 15, 14, 14, 14, 14, 14, 14, 14,
        14, 14, 14, 14, 14, 16, 16, 18, 18, 19,
        19, 22, 19, 19, 18, 24,
    ]

    def _sheet_wallets(self, ws, wallets: List[WalletStats]) -> None:
        ws.sheet_view.showGridLines = False
        ws.freeze_panes = "B3"
        ws.sheet_properties.tabColor = _GREEN[2:]
        ws.row_dimensions[1].height = 28
        ws.row_dimensions[2].height = 20

        # row 1: branding
        ws["A1"] = "Froggy Scanner"
        ws["A1"].font = Font(name="Calibri", size=13, bold=True, color=_GREEN)
        ws.merge_cells("A1:Z1")
        ws["A1"].fill = _fill(_BG_HEADER)

        # row 2: column headers
        for col, (hdr, width) in enumerate(
            zip(self._WALLET_HEADERS, self._WALLET_COL_WIDTHS), start=1
        ):
            cell = ws.cell(row=2, column=col, value=hdr)
            cell.font = _hdr_font()
            cell.fill = _fill(_BG_HEADER)
            cell.alignment = _centre()
            cell.border = _thin_border()
            ws.column_dimensions[get_column_letter(col)].width = width

        # data rows start at 3 (matching original file layout)
        for r_idx, w in enumerate(wallets, start=3):
            bg = _BG_ROW_ALT if r_idx % 2 == 0 else _BG_DARK
            row_data = [
                w.address,
                round(w.sol_balance, 6),
                round(w.usd_balance, 2),
                round(w.winrate, 10),
                round(w.roi / 100, 10),       # stored as decimal like original
                round(w.median_roi / 100, 10),
                round(w.avg_roi / 100, 10),
                round(w.pnl_sol, 9),
                round(w.spent_sol, 9),
                round(w.earned_sol, 9),
                round(w.smtb_spl, 10),
                w.tokens_count,
                round(w.avg_buys, 0),
                round(w.avg_sells, 0),
                round(w.avg_sell_pct, 10),
                round(w.median_sell_pct, 4),
                round(w.avg_spl_holdings, 10),
                round(w.holding_positions / w.tokens_count, 10)
                    if w.tokens_count else 0,
                seconds_to_human(w.median_trade_duration),
                seconds_to_human(w.avg_trade_duration),
                seconds_to_human(w.avg_duration_to_first_sell),
                _ts_to_excel_serial(w.last_swap_ts),
                _ts_to_excel_serial(w.first_swap_ts),
                round(w.avg_swap_fee_sol, 12),
                round(w.median_avg_swap_fee_sol, 12),
                round(w.multi_trans_swaps_pct, 10),
            ]

            for c_idx, value in enumerate(row_data, start=1):
                cell = ws.cell(row=r_idx, column=c_idx, value=value)
                cell.fill = _fill(bg)
                cell.border = _thin_border()
                cell.alignment = _centre()

                # wallet address: left-align
                if c_idx == 1:
                    cell.font = Font(name="Calibri", size=9, color=_LINK)
                    cell.alignment = Alignment(horizontal="left", vertical="center")
                    continue

                # PNL column (col 8 = H) — green / red
                if c_idx == 8:
                    color = _GREEN if (value or 0) >= 0 else _RED
                    cell.font = _cell_font(color, bold=True)
                    continue

                # date columns (V, W = col 22, 23) — format as date
                if c_idx in (22, 23) and value:
                    cell.number_format = "YYYY-MM-DD HH:MM"
                    cell.font = _cell_font(_GREY)
                    continue

                cell.font = _cell_font()

    # ── Token Trades sheet (extended mode) ────────────────────────────────

    _TOKEN_HEADERS = [
        "Wallet",
        "Token Mint",
        "Buy (SOL)",
        "Sell (SOL)",
        "PNL (SOL)",
        "ROI %",
        "Buy Count",
        "Sell Count",
        "Sell %",
        "First Trade",
        "Last Trade",
        "Duration",
        "Time to First Sell",
        "DEX",
        "Aggregator",
        "GMGN Link",
    ]

    _TOKEN_COL_WIDTHS = [
        46, 46, 13, 13, 13, 10, 9, 9, 9,
        20, 20, 16, 20, 18, 14, 56,
    ]

    def _sheet_tokens(self, ws, wallets: List[WalletStats]) -> None:
        ws.sheet_view.showGridLines = False
        ws.freeze_panes = "A2"
        ws.sheet_properties.tabColor = _GOLD[2:]

        # header row
        for col, (hdr, width) in enumerate(
            zip(self._TOKEN_HEADERS, self._TOKEN_COL_WIDTHS), start=1
        ):
            cell = ws.cell(row=1, column=col, value=hdr)
            cell.font = _hdr_font()
            cell.fill = _fill(_BG_HEADER)
            cell.alignment = _centre()
            cell.border = _thin_border()
            ws.column_dimensions[get_column_letter(col)].width = width

        r_idx = 2
        for w in wallets:
            for t in w.token_trades:
                bg = _BG_ROW_ALT if r_idx % 2 == 0 else _BG_DARK
                row_data = [
                    w.address,
                    t.token_mint,
                    round(t.buy_sol, 6),
                    round(t.sell_sol, 6),
                    round(t.pnl_sol, 6),
                    round(t.roi_pct, 2),
                    t.buy_count,
                    t.sell_count,
                    round(t.sell_pct * 100, 1),
                    _ts_to_str(t.first_trade_ts),
                    _ts_to_str(t.last_trade_ts),
                    seconds_to_human(t.trade_duration),
                    seconds_to_human(t.time_to_first_sell),
                    ", ".join(t.dexes) if t.dexes else "Unknown",
                    ", ".join(t.aggregators) if t.aggregators else "Direct",
                    t.gmgn_url,  # GMGN link — set as hyperlink below
                ]

                for c_idx, value in enumerate(row_data, start=1):
                    cell = ws.cell(row=r_idx, column=c_idx, value=value)
                    cell.fill = _fill(bg)
                    cell.border = _thin_border()
                    cell.alignment = _centre()

                    # wallet + mint: left-align
                    if c_idx in (1, 2):
                        cell.font = Font(name="Calibri", size=9, color=_GREY)
                        cell.alignment = Alignment(horizontal="left", vertical="center")
                        continue

                    # PNL column (col 5)
                    if c_idx == 5:
                        color = _GREEN if (value or 0) >= 0 else _RED
                        cell.font = _cell_font(color, bold=True)
                        continue

                    # GMGN Link column (col 16) — clickable hyperlink
                    if c_idx == 16 and value:
                        cell.hyperlink = value
                        cell.value = "→ GMGN"
                        cell.font = Font(
                            name="Calibri", size=9, bold=True,
                            color=_LINK, underline="single",
                        )
                        cell.alignment = _centre()
                        continue

                    cell.font = _cell_font()

                r_idx += 1

    # ── TXT ───────────────────────────────────────────────────────────────

    def _write_txt(self, wallets: List[WalletStats], path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write("=" * 80 + "\n")
            f.write("  Froggy Scanner — Solana Wallet Parser Report\n")
            f.write(f"  Generated : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"  Wallets   : {len(wallets)}\n")
            f.write("=" * 80 + "\n\n")

            for i, w in enumerate(wallets, 1):
                f.write(f"[{i}] {w.address}\n")
                f.write(f"    SOL Balance   : {w.sol_balance:.4f} SOL"
                        f"  (${w.usd_balance:.2f})\n")
                f.write(f"    Winrate       : {w.winrate * 100:.1f}%\n")
                f.write(f"    PNL           : {w.pnl_sol:+.4f} SOL\n")
                f.write(f"    ROI / Med / Avg: {w.roi:.1f}% / "
                        f"{w.median_roi:.1f}% / {w.avg_roi:.1f}%\n")
                f.write(f"    Tokens        : {w.tokens_count}"
                        f"  |  Trades: {w.total_trades}\n")
                f.write(f"    Rockets       : x2={w.rockets_x2}"
                        f"  x5={w.rockets_x5}  x10={w.rockets_x10}\n")
                f.write(f"    Last swap     : {_time_ago(w.last_swap_ts)}\n")
                f.write(f"    First swap    : {_time_ago(w.first_swap_ts)}\n")
                f.write(f"    Providers     : {', '.join(w.trade_providers) or '—'}\n")
                f.write(f"    Aggregators   : {', '.join(w.aggregator_counts.keys()) or '—'}\n")
                f.write(f"    Trade dur avg : {seconds_to_human(w.avg_trade_duration)}\n")
                f.write("\n    ── Token Trades ──\n")

                for t in w.token_trades:
                    sign = "+" if t.pnl_sol >= 0 else ""
                    f.write(
                        f"    {t.token_mint[:12]}…  "
                        f"Buy {t.buy_sol:.4f} SOL  "
                        f"Sell {t.sell_sol:.4f} SOL  "
                        f"PNL {sign}{t.pnl_sol:.4f} SOL  "
                        f"ROI {t.roi_pct:+.1f}%  "
                        f"DEX: {', '.join(t.dexes) or 'Unknown'}\n"
                        f"      GMGN: {t.gmgn_url}\n"
                    )

                f.write("\n" + "─" * 80 + "\n\n")
