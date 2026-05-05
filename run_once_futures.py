"""
Point d'entrée Bot 2 — un cycle horaire.

Étapes du cycle :
  1. Initialise DB + compte si premier run
  2. Surveillance des positions ouvertes (SL/TP/time stop/funding)
  3. Scan des opportunités (scanner_futures.run_scan)
  4. Ouvre les nouvelles positions filtrées par règles risk (max positions)
  5. Snapshot equity + résumé Telegram

Tournera en local (PC allumé) ou en GitHub Actions cron `0 * * * *`.

Architecture non-bloquante : chaque étape isolée en try/except. Une erreur
n'empêche pas le résumé final d'être envoyé (au contraire, il aide au debug).
"""

import io
import logging
import os
import sys
import traceback
from datetime import datetime, timezone

# UTF-8 stdout (pour les emojis et accents en GitHub Actions / Windows)
try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
except AttributeError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

_CHECKPOINT = "demarrage"

try:
    _CHECKPOINT = "imports"
    import alertes_futures as alertes
    import btc_regime_filter
    import config_futures as cfg
    import paper_account as account
    import paper_executor as executor
    import position_manager_futures as pm
    import scanner_futures as scanner
except Exception as _e:
    _tb = traceback.format_exc()
    print(f"[CRASH imports] {_e}\n{_tb}", file=sys.stderr)
    # Pas d'alerte Telegram ici — module alertes peut être celui qui a planté
    sys.exit(1)


# ─── Sécurités ────────────────────────────────────────────────────────────────

MAX_OPEN_POSITIONS = 4
MIN_MARGIN_FREE_PCT = 0.05  # garde 5% du capital libre


def can_open_more(open_positions: list[dict], state: dict) -> bool:
    if len(open_positions) >= MAX_OPEN_POSITIONS:
        logger.info(f"Cap positions atteint ({MAX_OPEN_POSITIONS}) — pas d'entrée")
        return False
    free_pct = (state["equity"] - state["margin_used"]) / state["equity"]
    if free_pct < MIN_MARGIN_FREE_PCT:
        logger.info(f"Marge libre {free_pct:.0%} < {MIN_MARGIN_FREE_PCT:.0%} — pas d'entrée")
        return False
    return True


# ─── Cycle ────────────────────────────────────────────────────────────────────

def run_cycle():
    global _CHECKPOINT

    now = datetime.now(timezone.utc)
    print(f"\n{'='*60}")
    print(f"  Bot 2 — CYCLE {now.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"{'='*60}\n")

    # 1. Init compte
    _CHECKPOINT = "init_account"
    account.init_db()
    executor.init_db()
    state = account.get_state()
    logger.info(
        f"Compte : equity ${state['equity']:.2f} | margin ${state['margin_used']:.2f} | "
        f"free ${state['equity'] - state['margin_used']:.2f}"
    )

    # 2. Régime BTC (cache pour le cycle)
    _CHECKPOINT = "btc_regime"
    btc_regime = btc_regime_filter.get_regime()

    # 3. Surveillance positions ouvertes (sorties SL/TP/time/funding)
    _CHECKPOINT = "position_management"
    print("\n--- GESTION POSITIONS ---")
    actions = []
    try:
        pm_result = pm.run()
        actions = pm_result.get("actions", [])
        if actions:
            for trade in actions:
                logger.info(
                    f"Sortie {trade['ticker']} {trade['side']} — {trade['exit_reason']}"
                )
                try:
                    alertes.alerte_close_position(trade)
                except Exception as e:
                    logger.warning(f"Alerte fermeture {trade['ticker']} échouée : {e}")
        else:
            logger.info("Pas de sortie ce cycle")
    except Exception as e:
        logger.error(f"Erreur gestion positions : {e}")
        traceback.print_exc()

    # Recharger état après sorties (margin_used a baissé)
    state = account.get_state()

    # 4. Scanner — chercher de nouvelles opportunités
    _CHECKPOINT = "scan"
    print("\n--- SCAN OPPORTUNITES ---")
    signals = []
    try:
        signals = scanner.run_scan()  # scan COMPLET des 99 paires
    except Exception as e:
        logger.error(f"Erreur scan : {e}")
        traceback.print_exc()

    # 5. Ouverture de nouvelles positions (sécurités)
    _CHECKPOINT = "open_positions"
    print("\n--- OUVERTURES ---")
    signals_taken = []
    open_positions = executor.get_open_positions()

    for sig in signals:
        if not can_open_more(open_positions, state):
            break
        # On n'ouvre pas un trade sur un ticker déjà en position
        if any(p["ticker"] == sig["ticker"] for p in open_positions):
            logger.info(f"{sig['ticker']} déjà en position — skip")
            continue
        try:
            trade = executor.open_position(
                ticker=sig["ticker"], side=sig["side"], score=sig["score"],
                inst_id=sig["inst_id"],
            )
            if trade:
                signals_taken.append(sig)
                open_positions.append(trade)
                state = account.get_state()  # refresh après ouverture
                try:
                    alertes.alerte_open_position(trade)
                except Exception as e:
                    logger.warning(f"Alerte ouverture {sig['ticker']} échouée : {e}")
        except Exception as e:
            logger.error(f"Ouverture {sig['ticker']} échouée : {e}")

    # 6. Snapshot equity curve
    _CHECKPOINT = "snapshot"
    try:
        account.snapshot_equity_curve()
    except Exception as e:
        logger.warning(f"Snapshot equity échoué : {e}")

    # 7. Résumé Telegram
    _CHECKPOINT = "resume"
    print("\n--- RESUME ---")
    state = account.get_state()
    open_positions = executor.get_open_positions()

    print(f"Equity   : ${state['equity']:.2f}")
    print(f"Margin   : ${state['margin_used']:.2f}")
    print(f"Trades   : {state['nb_trades_total']} (W:{state['nb_wins']}/L:{state['nb_losses']})")
    print(f"Open     : {len(open_positions)}")
    print(f"Signaux  : {len(signals)} détectés, {len(signals_taken)} pris")
    print(f"Sorties  : {len(actions)}")

    try:
        alertes.alerte_cycle_summary(
            account_state=state,
            open_positions=open_positions,
            actions=actions,
            signals_taken=signals_taken,
            btc_regime=btc_regime,
        )
        print("Résumé Telegram envoyé.")
    except Exception as e:
        logger.warning(f"Résumé Telegram échoué : {e}")

    print(f"\n{'='*60}")
    print(f"  Cycle terminé : {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}")
    print(f"{'='*60}\n")


# ─── Main ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        run_cycle()
    except Exception as e:
        _tb = traceback.format_exc()
        logger.error(f"CRASH cycle [checkpoint={_CHECKPOINT}] : {e}\n{_tb}")
        try:
            alertes.alerte_crash(_CHECKPOINT, f"{e}\n{_tb[-1500:]}")
        except Exception:
            pass
        sys.exit(1)
