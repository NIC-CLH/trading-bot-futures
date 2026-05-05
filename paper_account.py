"""
État du compte simulé Bot 2 — singleton persisté en SQLite.

Source de vérité unique :
  - equity            : capital actuel (initial + somme P&L net)
  - margin_used       : marge engagée par les positions ouvertes
  - peak_equity       : max equity historique (pour calcul drawdown)
  - max_drawdown_pct  : pire drawdown jamais traversé
  - nb_trades_total / nb_wins / nb_losses

À chaque ouverture/fermeture/funding, paper_executor appelle update_equity().
"""

import logging
import os
import sqlite3
import time

import config_futures as cfg

logger = logging.getLogger(__name__)

# Crée le dossier data/ si absent
_DB_DIR = os.path.dirname(cfg.DB_PATH)
if _DB_DIR:
    os.makedirs(_DB_DIR, exist_ok=True)


def init_db():
    """Crée les tables si elles n'existent pas + initialise le singleton."""
    with sqlite3.connect(cfg.DB_PATH) as conn:
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS paper_account (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                capital_initial REAL,
                equity REAL,
                margin_used REAL,
                peak_equity REAL,
                max_drawdown_pct REAL,
                nb_trades_total INTEGER,
                nb_wins INTEGER,
                nb_losses INTEGER,
                last_update_ts INTEGER
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS paper_equity_curve (
                ts INTEGER PRIMARY KEY,
                equity REAL,
                margin_used REAL,
                nb_open_positions INTEGER
            )
        """)
        c.execute("SELECT COUNT(*) FROM paper_account")
        if c.fetchone()[0] == 0:
            c.execute("""
                INSERT INTO paper_account (id, capital_initial, equity, margin_used,
                    peak_equity, max_drawdown_pct, nb_trades_total, nb_wins, nb_losses,
                    last_update_ts)
                VALUES (1, ?, ?, 0, ?, 0, 0, 0, 0, ?)
            """, (cfg.CAPITAL_INITIAL_VIRTUEL, cfg.CAPITAL_INITIAL_VIRTUEL,
                  cfg.CAPITAL_INITIAL_VIRTUEL, int(time.time())))
        conn.commit()


def get_state() -> dict:
    """Retourne l'état actuel du compte (singleton)."""
    init_db()
    with sqlite3.connect(cfg.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
        return dict(row)


def get_margin_free() -> float:
    """Capital disponible pour ouvrir une nouvelle position."""
    s = get_state()
    return s["equity"] - s["margin_used"]


def update_equity(equity_delta: float, margin_delta: float = 0.0,
                  trade_pnl: float | None = None):
    """
    Met à jour l'equity, la marge et les compteurs.

    Args:
        equity_delta : delta d'equity à appliquer (P&L net, fees, funding)
        margin_delta : delta de margin_used (+ à l'ouverture, − à la fermeture)
        trade_pnl    : si != None → c'est une clôture de trade (incrémente W/L)
    """
    init_db()
    with sqlite3.connect(cfg.DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        s = dict(conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone())

        new_equity = s["equity"] + equity_delta
        new_margin = max(0.0, s["margin_used"] + margin_delta)
        new_peak = max(s["peak_equity"], new_equity)
        new_dd = ((new_peak - new_equity) / new_peak * 100) if new_peak > 0 else 0.0
        new_max_dd = max(s["max_drawdown_pct"], new_dd)

        new_trades = s["nb_trades_total"] + (1 if trade_pnl is not None else 0)
        new_wins = s["nb_wins"] + (1 if trade_pnl is not None and trade_pnl > 0 else 0)
        new_losses = s["nb_losses"] + (1 if trade_pnl is not None and trade_pnl <= 0 else 0)

        conn.execute("""
            UPDATE paper_account SET
                equity=?, margin_used=?, peak_equity=?, max_drawdown_pct=?,
                nb_trades_total=?, nb_wins=?, nb_losses=?, last_update_ts=?
            WHERE id=1
        """, (new_equity, new_margin, new_peak, new_max_dd,
              new_trades, new_wins, new_losses, int(time.time())))
        conn.commit()


def snapshot_equity_curve():
    """Enregistre l'equity actuelle dans la courbe (1 point par appel)."""
    init_db()
    s = get_state()
    with sqlite3.connect(cfg.DB_PATH) as conn:
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM paper_trades WHERE status='open'"
            ).fetchone()[0]
        except sqlite3.OperationalError:
            n = 0  # paper_trades pas encore créée
        conn.execute("""
            INSERT OR REPLACE INTO paper_equity_curve (ts, equity, margin_used, nb_open_positions)
            VALUES (?, ?, ?, ?)
        """, (int(time.time()), s["equity"], s["margin_used"], n))
        conn.commit()


def reset_account():
    """RAZ totale (supprime la base). À utiliser entre tests."""
    if os.path.exists(cfg.DB_PATH):
        os.remove(cfg.DB_PATH)
    init_db()


def winrate() -> float | None:
    s = get_state()
    if s["nb_trades_total"] == 0:
        return None
    return s["nb_wins"] / s["nb_trades_total"]


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=" * 60)
    print("  paper_account.py — smoke test")
    print("=" * 60)

    reset_account()
    s = get_state()
    print(f"\n[1] État initial :")
    for k, v in s.items():
        print(f"    {k}: {v}")
    print(f"    margin_free: ${get_margin_free():.2f}")

    # Simule l'ouverture d'une position : margin +42.5, fees -0.04
    print(f"\n[2] Simule ouverture (margin +42.5, fees -0.04) :")
    update_equity(equity_delta=-0.04, margin_delta=42.5)
    s = get_state()
    print(f"    equity={s['equity']:.4f} | margin={s['margin_used']:.2f} | "
          f"free={get_margin_free():.2f}")

    # Simule la fermeture profit : margin libérée, P&L net +3.4
    print(f"\n[3] Simule fermeture TP (margin -42.5, P&L +3.4) :")
    update_equity(equity_delta=3.4, margin_delta=-42.5, trade_pnl=3.4)
    s = get_state()
    print(f"    equity={s['equity']:.4f} | margin={s['margin_used']:.2f} | "
          f"free={get_margin_free():.2f} | trades={s['nb_trades_total']} "
          f"(W:{s['nb_wins']}/L:{s['nb_losses']})")
    print(f"    peak=${s['peak_equity']:.2f} | max_dd={s['max_drawdown_pct']:.2f}%")
    print(f"    winrate: {winrate():.0%}")

    print("\n" + "=" * 60)
