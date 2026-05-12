"""
Filtre directionnel BTC — version améliorée pour Bot 2.

Bot 1 : règle binaire "BTC < MA50_daily → pas d'achat".
Problème : sur 1h près de la MA50, ça flip 5-10× par jour (whipsaw).

Bot 2 :
  - Calcul sur bougies 4H (lisse les wicks 1h, plus stable)
  - Bande d'hystérésis ±1.5% autour de la MA50 (zone neutre, pas de flip)
  - Cooldown 4H minimum entre 2 changements d'état
  - Inversion long/short selon l'état (Bot 1 ne fait que bloquer les longs)

Etats :
  bull  → longs autorisés, shorts bloqués
  bear  → shorts autorisés, longs bloqués
  neutral  → both autorisés (au démarrage, ou avant le 1er flip)

Persistance : data/btc_regime_state.json
"""

import json
import logging
import os
import time

import okx_futures as okx

logger = logging.getLogger(__name__)

STATE_PATH = "data/btc_regime_state.json"
HYST_BAND = 0.015          # ±1.5% autour de la MA50 = bande d'hystérésis pour state
STRONG_BAND = 0.03         # ±3% = seuil "very bull" / "very bear" pour blocage directionnel
COOLDOWN_HOURS = 4         # pas de flip dans les 4h suivant un changement
MA_PERIOD = 50

os.makedirs("data", exist_ok=True)


# ─── Persistance ──────────────────────────────────────────────────────────────

def _load_state() -> dict:
    if not os.path.exists(STATE_PATH):
        return {"state": "neutral", "last_flip_ts": 0}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Lecture état régime : {e} — reset")
        return {"state": "neutral", "last_flip_ts": 0}


def _save_state(state: dict) -> None:
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f)


# ─── API publique ─────────────────────────────────────────────────────────────

def get_regime() -> dict:
    """
    Calcule + persiste le régime BTC courant.

    Returns:
        {
          "state":         "bull" | "bear" | "neutral",
          "btc_price":     float,
          "ma50_4h":       float,
          "deviation_pct": float,         # signe : > 0 si BTC au-dessus
          "allow_long":    bool,
          "allow_short":   bool,
          "last_flip_ts":  int,           # epoch du dernier changement d'état
        }

    Stratégie :
      1. Récupère 60 bougies 4h (suffisant pour MA50 + buffer)
      2. Calcule deviation = (BTC − MA50) / MA50
      3. Etat candidat :
         - dev > +1.5% → 'bull'
         - dev < −1.5% → 'bear'
         - sinon       → garde l'état précédent (sticky)
      4. Si l'état candidat diffère de l'actuel :
         - vérifie le cooldown (4h depuis dernier flip)
         - si OK → flip + persist
         - sinon → garde l'état actuel, log l'attempt
    """
    df = okx.get_ohlcv("BTC-USDT-SWAP", bar="4H", limit=MA_PERIOD + 10)
    if df.empty or len(df) < MA_PERIOD:
        logger.warning("BTC OHLCV 4h insuffisant — régime forcé neutral")
        return {
            "state": "neutral", "btc_price": 0.0, "ma50_4h": 0.0,
            "deviation_pct": 0.0,
            "allow_long": True, "allow_short": True,
            "last_flip_ts": 0,
        }

    btc_price = float(df["close"].iloc[-1])
    ma50 = float(df["close"].rolling(MA_PERIOD).mean().iloc[-1])
    deviation = (btc_price - ma50) / ma50  # > 0 → au-dessus

    saved = _load_state()
    prev_state = saved.get("state", "neutral")
    last_flip_ts = int(saved.get("last_flip_ts", 0) or 0)
    now = int(time.time())
    cooldown_active = (now - last_flip_ts) < COOLDOWN_HOURS * 3600

    # ── Etat candidat selon la deviation ────────────────────────────────────
    if deviation > HYST_BAND:
        candidate = "bull"
    elif deviation < -HYST_BAND:
        candidate = "bear"
    else:
        # Dans la bande neutre → maintien (sticky)
        candidate = prev_state if prev_state != "neutral" else "neutral"

    # ── Application du cooldown ──────────────────────────────────────────────
    if candidate != prev_state and cooldown_active and prev_state != "neutral":
        remaining = (COOLDOWN_HOURS * 3600) - (now - last_flip_ts)
        logger.info(
            f"BTC régime : flip {prev_state}→{candidate} BLOQUE "
            f"(cooldown {remaining // 60} min restantes) | "
            f"dev {deviation:+.2%}"
        )
        new_state = prev_state
    else:
        new_state = candidate

    if new_state != prev_state:
        last_flip_ts = now
        logger.info(
            f"BTC régime : {prev_state} → {new_state} "
            f"(price ${btc_price:,.0f} | MA50_4h ${ma50:,.0f} | dev {deviation:+.2%})"
        )

    _save_state({"state": new_state, "last_flip_ts": last_flip_ts})

    # Mapping état → autorisations directionnelles (partiel)
    # Bug détecté au bilan : en bull (+4%) on a pris 7 shorts perdants (12% WR).
    # Nouveau garde-fou : ne bloquer la direction QUE si BTC est "very bull/bear"
    # (déviation >= ±3% au-dessus/dessous MA50_4h). Entre les deux : both autorisés.
    abs_dev = abs(deviation)
    if new_state == "bull" and abs_dev >= STRONG_BAND:
        allow_long, allow_short = True, False
    elif new_state == "bear" and abs_dev >= STRONG_BAND:
        allow_long, allow_short = False, True
    else:
        # bull/bear faible OU neutral → both autorisés (le scoring filtre)
        allow_long, allow_short = True, True

    return {
        "state":         new_state,
        "btc_price":     btc_price,
        "ma50_4h":       ma50,
        "deviation_pct": round(deviation * 100, 2),
        "allow_long":    allow_long,
        "allow_short":   allow_short,
        "last_flip_ts":  last_flip_ts,
    }


def is_direction_allowed(side: str) -> bool:
    """Check rapide pour le scanner. side ∈ {'long', 'short'}"""
    r = get_regime()
    return r["allow_long"] if side == "long" else r["allow_short"]


def reset_state():
    """RAZ état (pour tests ou redémarrage propre)."""
    if os.path.exists(STATE_PATH):
        os.remove(STATE_PATH)


# ─── Smoke test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=" * 60)
    print("  btc_regime_filter.py — état directionnel BTC")
    print("=" * 60)

    r = get_regime()
    print(f"\n  Etat         : {r['state'].upper()}")
    print(f"  BTC price    : ${r['btc_price']:,.0f}")
    print(f"  MA50 4h      : ${r['ma50_4h']:,.0f}")
    print(f"  Deviation    : {r['deviation_pct']:+.2f}%  (bande hystérésis ±1.5%)")
    print(f"  Allow LONG   : {r['allow_long']}")
    print(f"  Allow SHORT  : {r['allow_short']}")
    if r["last_flip_ts"]:
        from datetime import datetime, timezone
        flip_dt = datetime.fromtimestamp(r["last_flip_ts"], tz=timezone.utc)
        age_h = (time.time() - r["last_flip_ts"]) / 3600
        print(f"  Dernier flip : {flip_dt.strftime('%Y-%m-%d %H:%M UTC')} ({age_h:.1f}h ago)")
    print("\n" + "=" * 60)
