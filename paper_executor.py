"""
Moteur d'exécution simulée Bot 2.

Simule réalistiquement les entrées/sorties sur perps OKX :
  - Fees taker 0.05% par côté
  - Slippage 0.05% à l'entrée et à la sortie
  - Funding rate appliqué à chaque slot 8h (00/08/16 UTC)
  - Liquidation = entry × (1 ∓ 1/levier ± maintenance_margin)

Sizing par tier de score (mêmes ratios que Bot 1) :
  score >= 2.5 : marge 22% du portfolio | levier 3x | notional 66%
  score >= 2.0 : marge 17%               | levier 2x | notional 34%
  score >= 1.7 : marge 12%               | levier 2x | notional 24%

Risque max théorique par trade (avec stop -3% sur prix) :
  notional 24% × 3% = 0.72% du portfolio
  notional 34% × 3% = 1.02%
  notional 66% × 3% = 1.98%

Persistance : SQLite paper_trades.db (table paper_trades).
"""

import logging
import sqlite3
import time

import config_futures as cfg
import okx_futures as okx
import paper_account as account

logger = logging.getLogger(__name__)


def init_db():
    """Crée la table paper_trades."""
    with sqlite3.connect(cfg.DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS paper_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT,
                inst_id TEXT,
                side TEXT,
                entry_ts INTEGER,
                entry_price REAL,
                exit_ts INTEGER,
                exit_price REAL,
                qty REAL,
                notional_usdt REAL,
                margin_usdt REAL,
                leverage REAL,
                sl_price REAL,
                tp_price REAL,
                liq_price REAL,
                max_hold_ts INTEGER,
                fees_total_usdt REAL,
                slippage_total_usdt REAL,
                funding_paid_usdt REAL DEFAULT 0,
                pnl_gross_usdt REAL,
                pnl_net_usdt REAL,
                pnl_pct_margin REAL,
                pnl_pct_notional REAL,
                score REAL,
                last_funding_check_ts INTEGER,
                exit_reason TEXT,
                status TEXT DEFAULT 'open'
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON paper_trades(status)")
        conn.commit()


# ── Calculs déterministes ────────────────────────────────────────────────────

def calculate_liquidation_price(entry_price: float, leverage: float, side: str) -> float:
    """
    Prix de liquidation théorique en isolated margin.
        Long  : entry × (1 − 1/leverage + MM)
        Short : entry × (1 + 1/leverage − MM)
    """
    mm = cfg.MAINTENANCE_MARGIN_PCT
    if side == "long":
        return entry_price * (1 - 1 / leverage + mm)
    return entry_price * (1 + 1 / leverage - mm)


def calculate_size(score_abs: float, equity: float) -> tuple[float, float, float]:
    """
    Retourne (margin_pct, leverage, notional_pct).
    Tiers Bot 1 + levier conditionnel sur le tier A+.
    """
    if score_abs >= cfg.SCORE_A_PLUS:
        return 0.22, cfg.LEVIER_MAX, 0.22 * cfg.LEVIER_MAX
    if score_abs >= 2.0:
        return 0.17, cfg.LEVIER_DEFAUT, 0.17 * cfg.LEVIER_DEFAUT
    return 0.12, cfg.LEVIER_DEFAUT, 0.12 * cfg.LEVIER_DEFAUT


# ── Open / close ──────────────────────────────────────────────────────────────

def open_position(ticker: str, side: str, score: float,
                   inst_id: str | None = None) -> dict | None:
    """
    Ouvre une position simulée long ou short.

    Args:
        ticker  : ex 'BTC'
        side    : 'long' | 'short'
        score   : score composite signé (+ pour long, − pour short en théorie)
        inst_id : ex 'BTC-USDT-SWAP' (déduit du ticker si omis)

    Retourne le dict du trade ouvert, ou None si refus (marge, ticker, etc.).
    """
    init_db()
    if not inst_id:
        inst_id = f"{ticker.upper()}-USDT-SWAP"
    side = side.lower()
    if side not in ("long", "short"):
        logger.error(f"side invalide : {side}")
        return None

    score_abs = abs(score)
    if score_abs < cfg.SCORE_MIN:
        logger.info(f"{ticker} : score {score:+.2f} < {cfg.SCORE_MIN} — refus")
        return None

    state = account.get_state()
    margin_free = account.get_margin_free()
    margin_pct, leverage, _ = calculate_size(score_abs, state["equity"])

    margin = state["equity"] * margin_pct
    notional = margin * leverage

    if margin > margin_free * 0.95:
        logger.warning(
            f"{ticker} : marge cible ${margin:.2f} > marge libre ${margin_free:.2f} — refus"
        )
        return None

    # Prix actuel + slippage
    tk = okx.get_ticker(inst_id)
    if not tk or not tk.get("last"):
        logger.warning(f"{ticker} ({inst_id}) : ticker indisponible — refus")
        return None
    mid = tk["last"]
    entry_price = mid * (1 + cfg.SLIPPAGE_PCT) if side == "long" else mid * (1 - cfg.SLIPPAGE_PCT)
    qty = notional / entry_price

    # SL / TP
    if side == "long":
        sl_price = entry_price * (1 - cfg.SL_PCT)
        tp_price = entry_price * (1 + cfg.TP_PCT)
    else:
        sl_price = entry_price * (1 + cfg.SL_PCT)
        tp_price = entry_price * (1 - cfg.TP_PCT)

    liq_price = calculate_liquidation_price(entry_price, leverage, side)
    now = int(time.time())
    max_hold_ts = now + cfg.MAX_HOLDING_HOURS * 3600

    fees_entry = notional * cfg.FEES_TAKER_PCT
    slippage_cost = notional * cfg.SLIPPAGE_PCT  # déjà appliqué dans entry_price, gardé pour stat

    # Persistance
    with sqlite3.connect(cfg.DB_PATH) as conn:
        cur = conn.execute("""
            INSERT INTO paper_trades (
                ticker, inst_id, side, entry_ts, entry_price, qty,
                notional_usdt, margin_usdt, leverage,
                sl_price, tp_price, liq_price, max_hold_ts,
                fees_total_usdt, slippage_total_usdt, funding_paid_usdt,
                score, last_funding_check_ts, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, 'open')
        """, (
            ticker, inst_id, side, now, entry_price, qty,
            notional, margin, leverage,
            sl_price, tp_price, liq_price, max_hold_ts,
            fees_entry, slippage_cost,
            score, now,
        ))
        trade_id = cur.lastrowid
        conn.commit()

    # Compte : marge gelée + fees déduites
    account.update_equity(equity_delta=-fees_entry, margin_delta=margin)

    logger.info(
        f"[OPEN] {side.upper()} {ticker} @ ${entry_price:,.4f} | "
        f"qty={qty:.6f} | margin=${margin:.2f} (notional=${notional:.2f}, lev {leverage}x) | "
        f"SL ${sl_price:,.4f} | TP ${tp_price:,.4f} | LIQ ${liq_price:,.4f} | "
        f"score {score:+.2f}"
    )

    return {
        "id": trade_id, "ticker": ticker, "inst_id": inst_id, "side": side,
        "entry_ts": now, "entry_price": entry_price, "qty": qty,
        "notional_usdt": notional, "margin_usdt": margin, "leverage": leverage,
        "sl_price": sl_price, "tp_price": tp_price, "liq_price": liq_price,
        "max_hold_ts": max_hold_ts,
        "fees_total_usdt": fees_entry, "slippage_total_usdt": slippage_cost,
        "score": score,
    }


def close_position(trade_id: int, exit_price: float, reason: str) -> dict | None:
    """
    Ferme une position simulée et calcule le P&L net.

    P&L net = P&L brut (avec slippage exit) − fees exit − funding cumulé
    Note : les fees d'entrée ont déjà été déduites de l'equity à l'open.
    """
    with sqlite3.connect(cfg.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM paper_trades WHERE id=?", (trade_id,)
        ).fetchone()
        if not row:
            logger.warning(f"close_position : trade {trade_id} introuvable")
            return None
        if row["status"] != "open":
            logger.warning(f"close_position : trade {trade_id} déjà fermé")
            return None
        t = dict(row)

    side = t["side"]
    entry = t["entry_price"]
    qty = t["qty"]

    # Slippage de sortie
    if side == "long":
        actual_exit = exit_price * (1 - cfg.SLIPPAGE_PCT)
        pnl_gross = (actual_exit - entry) * qty
    else:
        actual_exit = exit_price * (1 + cfg.SLIPPAGE_PCT)
        pnl_gross = (entry - actual_exit) * qty

    notional_exit = abs(actual_exit * qty)
    fees_exit = notional_exit * cfg.FEES_TAKER_PCT
    slippage_exit = notional_exit * cfg.SLIPPAGE_PCT

    fees_total = (t["fees_total_usdt"] or 0) + fees_exit
    slippage_total = (t["slippage_total_usdt"] or 0) + slippage_exit
    funding_total = t["funding_paid_usdt"] or 0.0

    pnl_net = pnl_gross - fees_exit - funding_total
    margin = t["margin_usdt"] or 0
    pnl_pct_margin = (pnl_net / margin * 100) if margin else 0
    pnl_pct_notional = (pnl_net / (t["notional_usdt"] or 1) * 100)

    now = int(time.time())

    with sqlite3.connect(cfg.DB_PATH) as conn:
        conn.execute("""
            UPDATE paper_trades SET
                exit_ts=?, exit_price=?, fees_total_usdt=?, slippage_total_usdt=?,
                pnl_gross_usdt=?, pnl_net_usdt=?,
                pnl_pct_margin=?, pnl_pct_notional=?,
                exit_reason=?, status='closed'
            WHERE id=?
        """, (now, actual_exit, fees_total, slippage_total,
              pnl_gross, pnl_net, pnl_pct_margin, pnl_pct_notional,
              reason, trade_id))
        conn.commit()

    # Compte : marge libérée + P&L net appliqué
    account.update_equity(
        equity_delta=pnl_net,
        margin_delta=-margin,
        trade_pnl=pnl_net,
    )

    logger.info(
        f"[CLOSE] {side.upper()} {t['ticker']} @ ${actual_exit:,.4f} | "
        f"reason={reason} | gross=${pnl_gross:+.2f} | net=${pnl_net:+.2f} | "
        f"%marge={pnl_pct_margin:+.2f}%"
    )

    return {**t, "exit_ts": now, "exit_price": actual_exit,
            "pnl_gross_usdt": pnl_gross, "pnl_net_usdt": pnl_net,
            "pnl_pct_margin": pnl_pct_margin, "exit_reason": reason}


def apply_funding(trade_id: int, funding_rate: float):
    """
    Applique un cycle de funding (toutes les 8h sur OKX) à une position.
        Long  : paie  funding_rate × notional si rate > 0 (reçoit si rate < 0)
        Short : reçoit funding_rate × notional si rate > 0 (paie    si rate < 0)
    """
    with sqlite3.connect(cfg.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM paper_trades WHERE id=? AND status='open'", (trade_id,)
        ).fetchone()
        if not row:
            return
        t = dict(row)

    notional = t["notional_usdt"] or 0
    payment = notional * funding_rate if t["side"] == "long" else -notional * funding_rate
    new_total = (t["funding_paid_usdt"] or 0) + payment

    with sqlite3.connect(cfg.DB_PATH) as conn:
        conn.execute(
            "UPDATE paper_trades SET funding_paid_usdt=?, last_funding_check_ts=? WHERE id=?",
            (new_total, int(time.time()), trade_id)
        )
        conn.commit()

    account.update_equity(equity_delta=-payment)
    logger.debug(
        f"[FUND] trade {trade_id} {t['ticker']} : {payment:+.4f} USDT (rate {funding_rate:+.4%})"
    )


# ── Lookups ───────────────────────────────────────────────────────────────────

def get_open_positions() -> list[dict]:
    """Toutes les positions actuellement ouvertes."""
    init_db()
    with sqlite3.connect(cfg.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE status='open' ORDER BY entry_ts ASC"
        ).fetchall()
        return [dict(r) for r in rows]


def get_closed_trades(limit: int = 100) -> list[dict]:
    """Derniers trades fermés (P&L réalisé)."""
    init_db()
    with sqlite3.connect(cfg.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM paper_trades WHERE status='closed' "
            "ORDER BY exit_ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


# ── Smoke test : ouvre + ferme un trade BTC long ─────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=" * 60)
    print("  paper_executor.py — smoke test (ouvre + ferme BTC long)")
    print("=" * 60)

    account.reset_account()
    init_db()

    print("\n[1] État initial :")
    s = account.get_state()
    print(f"    equity=${s['equity']:.2f} | margin=${s['margin_used']:.2f} | "
          f"free=${account.get_margin_free():.2f}")

    print("\n[2] Ouverture LONG BTC, score +2.1 (tier 17%, levier 2x) :")
    trade = open_position(ticker="BTC", side="long", score=2.1)
    if not trade:
        print("    → ÉCHEC")
        raise SystemExit(1)

    s = account.get_state()
    print(f"\n[3] État après ouverture :")
    print(f"    equity=${s['equity']:.4f} | margin=${s['margin_used']:.2f} | "
          f"free=${account.get_margin_free():.2f}")

    # TP (+4%) atteint sur le prix → simule la sortie
    exit_at_tp = trade["entry_price"] * 1.04
    print(f"\n[4] Sortie au TP (+4% prix) : ${exit_at_tp:,.4f}")
    result = close_position(trade["id"], exit_at_tp, reason="tp")
    if not result:
        print("    → ÉCHEC")
        raise SystemExit(1)
    print(f"    P&L brut : ${result['pnl_gross_usdt']:+.4f}")
    print(f"    P&L net  : ${result['pnl_net_usdt']:+.4f}")
    print(f"    % marge  : {result['pnl_pct_margin']:+.2f}%")

    s = account.get_state()
    print(f"\n[5] État final :")
    print(f"    equity=${s['equity']:.4f} | margin=${s['margin_used']:.2f}")
    print(f"    trades total: {s['nb_trades_total']} (W:{s['nb_wins']}/L:{s['nb_losses']})")
    print(f"    peak=${s['peak_equity']:.4f} | max_dd={s['max_drawdown_pct']:.4f}%")
    if s["nb_trades_total"]:
        print(f"    winrate: {s['nb_wins']/s['nb_trades_total']:.0%}")

    print("\n" + "=" * 60)
