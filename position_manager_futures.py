"""
Gestionnaire de positions Bot 2 — surveillance & sorties.

Règles d'exit (par priorité) :
  P1 — Liquidation : prix touche liq_price                    → close 'liquidation'
  P2 — Stop loss   : prix touche sl_price                     → close 'sl'
  P3 — Take profit : prix touche tp_price                     → close 'tp'
  P4 — Time stop   : now >= max_hold_ts (24h après entrée)    → close 'time_stop'

Détection wick : on évalue avec le high/low de la dernière bougie 1h, pas
seulement le close. Si une bougie a touché SL ET TP dans le même range, on
prend SL en priorité (hypothèse défensive — on ne sait pas l'ordre intra-bougie).

Funding : appliqué à chaque slot 8h UTC (00/08/16) si on est passé après le
dernier check. Le rate utilisé = funding rate courant OKX.

Évalué à chaque cycle 1h.
"""

import logging
import time
from datetime import datetime, timezone, timedelta

import config_futures as cfg
import okx_futures as okx
import paper_account as account
import paper_executor as executor

logger = logging.getLogger(__name__)

FUNDING_HOURS_UTC = (0, 8, 16)


def _last_funding_slot_ts(now_ts: int) -> int:
    """Timestamp UTC du dernier slot funding (00/08/16) <= now_ts."""
    dt = datetime.fromtimestamp(now_ts, tz=timezone.utc)
    candidates = []
    for slot in FUNDING_HOURS_UTC:
        slot_dt = dt.replace(hour=slot, minute=0, second=0, microsecond=0)
        if slot_dt > dt:
            slot_dt -= timedelta(days=1)
        candidates.append(slot_dt)
    return int(max(candidates).timestamp())


def evaluate_position(pos: dict, current_price: float,
                      last_high: float | None = None,
                      last_low: float | None = None) -> dict:
    """
    Décide HOLD ou CLOSE pour une position.

    current_price : close de la dernière bougie (référence pour time_stop)
    last_high     : high de la dernière bougie (détection wick haut)
    last_low      : low de la dernière bougie  (détection wick bas)

    Retourne {'action': 'HOLD'|'CLOSE', 'exit_price': float|None, 'reason': str}
    """
    side = pos["side"]
    sl, tp, liq = pos["sl_price"], pos["tp_price"], pos["liq_price"]
    max_hold = pos["max_hold_ts"]
    now = int(time.time())

    h = last_high if last_high is not None else current_price
    l = last_low if last_low is not None else current_price

    # P1 — Liquidation
    if side == "long" and l <= liq:
        return {"action": "CLOSE", "exit_price": liq, "reason": "liquidation"}
    if side == "short" and h >= liq:
        return {"action": "CLOSE", "exit_price": liq, "reason": "liquidation"}

    # P2 — Stop loss (prioritaire sur TP en cas de double-touche dans la même bougie)
    if side == "long" and l <= sl:
        return {"action": "CLOSE", "exit_price": sl, "reason": "sl"}
    if side == "short" and h >= sl:
        return {"action": "CLOSE", "exit_price": sl, "reason": "sl"}

    # P3 — Take profit
    if side == "long" and h >= tp:
        return {"action": "CLOSE", "exit_price": tp, "reason": "tp"}
    if side == "short" and l <= tp:
        return {"action": "CLOSE", "exit_price": tp, "reason": "tp"}

    # P4 — Time stop
    if now >= max_hold:
        return {"action": "CLOSE", "exit_price": current_price, "reason": "time_stop"}

    return {"action": "HOLD", "exit_price": None, "reason": ""}


def check_funding(pos: dict):
    """Si on a passé un slot 8h depuis le dernier check, applique le funding."""
    last_check = pos.get("last_funding_check_ts", 0) or 0
    last_slot = _last_funding_slot_ts(int(time.time()))
    if last_slot > last_check:
        fund = okx.get_funding_rate(pos["inst_id"])
        rate = fund.get("current", 0)
        if rate != 0:
            executor.apply_funding(pos["id"], rate)
        else:
            logger.debug(f"{pos['ticker']} : funding rate 0 — pas d'application")


def run() -> dict:
    """
    Cycle de surveillance. Pour chaque position ouverte :
      1. Récupère OHLCV 1h (2 bougies)
      2. Applique funding si slot 8h passé
      3. Évalue HOLD/CLOSE sur high/low de la dernière bougie

    Retourne {'actions': [...], 'open': N} pour le résumé Telegram.
    """
    positions = executor.get_open_positions()
    if not positions:
        logger.info("Aucune position ouverte.")
        account.snapshot_equity_curve()
        return {"actions": [], "open": 0}

    actions = []
    for pos in positions:
        ohlcv = okx.get_ohlcv(pos["inst_id"], bar="1H", limit=2)
        if ohlcv.empty:
            logger.warning(f"OHLCV indisponible pour {pos['ticker']} — skip")
            continue

        last = ohlcv.iloc[-1]
        current_price = float(last["close"])
        last_high = float(last["high"])
        last_low = float(last["low"])

        check_funding(pos)

        decision = evaluate_position(pos, current_price, last_high, last_low)

        if decision["action"] == "CLOSE":
            result = executor.close_position(
                pos["id"], decision["exit_price"], decision["reason"]
            )
            if result:
                # Retourne le trade complet — run_once peut alerter directement
                # sans re-chercher en DB (évite race si 2 fermetures identiques).
                actions.append(result)
        else:
            # P&L latent (pour le log uniquement)
            if pos["side"] == "long":
                pnl_latent = (current_price - pos["entry_price"]) * pos["qty"]
            else:
                pnl_latent = (pos["entry_price"] - current_price) * pos["qty"]
            margin = pos["margin_usdt"] or 1
            pnl_pct = pnl_latent / margin * 100
            logger.info(
                f"[HOLD] {pos['side'].upper()} {pos['ticker']} | "
                f"prix=${current_price:,.4f} | latent=${pnl_latent:+.2f} ({pnl_pct:+.2f}%/marge)"
            )

    account.snapshot_equity_curve()
    return {"actions": actions, "open": len(positions) - len(actions)}


# ── Smoke test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=" * 60)
    print("  position_manager_futures.py — surveillance des positions")
    print("=" * 60)

    result = run()
    print(f"\nActions ce cycle : {len(result['actions'])}")
    print(f"Positions encore ouvertes : {result['open']}")
    for a in result["actions"]:
        print(f"  • {a['side'].upper()} {a['ticker']} → {a['reason']} | "
              f"P&L ${a['pnl_net_usdt']:+.2f} ({a['pnl_pct_margin']:+.2f}%/marge)")
    print("=" * 60)
