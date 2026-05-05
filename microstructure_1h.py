"""
Microstructure marché OKX SWAP — adapté de Bot 1 (qui utilisait SPOT).

5 indicateurs combinés sur perps OKX :
  1. Funding rate         (perp specific) — déjà géré dans funding_score.py
  2. Open Interest        (SWAP USDT)
  3. Long/Short ratio     (SWAP USDT, period 1h)
  4. Taker volume         (SWAP USDT, period 1h)
  5. Liquidations         (SWAP USDT, sides buy/sell)

Note : funding rate géré dans `funding_score.py` (7e dimension dédiée).
Ici on calcule les 4 autres composantes et on retourne un score combiné.

Score : -1.5 à +1.5 (intégré dans le score composite scanner avec poids ~20%).
"""

import logging

import okx_futures as okx

logger = logging.getLogger(__name__)

DEFAULT_PERIOD = "1H"  # Bot 1 utilisait "5m" — adapté au cycle 1h Bot 2


def _get_public(path: str, params: dict | None = None) -> list:
    """Wrapper sur okx._get(safe=True) — porte le header simulated-trading."""
    return okx._get(path, params, auth=False, safe=True)


# ─── Open Interest ────────────────────────────────────────────────────────────

def get_open_interest(inst_id: str) -> dict:
    """OI sur perp SWAP. Le score est calculé en combinaison avec le prix ailleurs."""
    data = _get_public("/api/v5/public/open-interest", {"instId": inst_id})
    if not data:
        return {"oi": None, "score": 0.0, "signal": "OI N/A"}
    try:
        oi_token = float(data[0].get("oiCcy", 0) or 0)
        oi_usd = float(data[0].get("oi", 0) or 0)
        return {
            "oi": round(oi_token, 0),
            "oi_usd": round(oi_usd, 0),
            "score": 0.0,
            "signal": f"OI: {oi_token:,.0f} contrats",
        }
    except Exception as e:
        logger.debug(f"OI {inst_id} : {e}")
        return {"oi": None, "score": 0.0, "signal": "OI N/A"}


# ─── Long/Short Ratio ────────────────────────────────────────────────────────

def get_long_short_ratio(inst_id: str, period: str = DEFAULT_PERIOD) -> dict:
    """
    L/S ratio sur perp SWAP. period = '5m' | '15m' | '1H' | '4H' | '1D'.
    Score contrarian : trop de longs = bearish, trop de shorts = bullish.
    """
    base = inst_id.split("-")[0]
    # OKX Rubik utilise le ticker spot pour le L/S des comptes traders
    data = _get_public(
        "/api/v5/rubik/stat/contracts/long-short-account-ratio",
        {"ccy": base, "period": period}
    )
    if not data:
        # Fallback : essai avec instId au cas où l'API a changé
        data = _get_public(
            "/api/v5/rubik/stat/contracts/long-short-account-ratio",
            {"instId": f"{base}-USDT", "period": period}
        )
    if not data:
        return {"ratio": None, "score": 0.0, "signal": "L/S N/A"}

    try:
        # Format Rubik : [[ts, ratio], ...] OU [{"ts":..., "longShortRatio":...}]
        first = data[0]
        if isinstance(first, list):
            ratio = float(first[1])
        else:
            ratio = float(first.get("longShortRatio", 1.0))

        if ratio > 2.5:
            score, signal = -1.0, f"L/S {ratio:.2f} - foule trop longue"
        elif ratio > 1.8:
            score, signal = -0.5, f"L/S {ratio:.2f} - majorité longue"
        elif ratio < 0.4:
            score, signal = 1.0, f"L/S {ratio:.2f} - short squeeze potentiel"
        elif ratio < 0.6:
            score, signal = 0.5, f"L/S {ratio:.2f} - majorité short"
        else:
            score, signal = 0.0, f"L/S {ratio:.2f} - équilibré"
        return {"ratio": round(ratio, 3), "score": score, "signal": signal}
    except Exception as e:
        logger.debug(f"L/S {inst_id} : {e}")
        return {"ratio": None, "score": 0.0, "signal": "L/S N/A"}


# ─── Taker Volume ─────────────────────────────────────────────────────────────

def get_taker_volume(inst_id: str, period: str = DEFAULT_PERIOD) -> dict:
    """
    Ratio buy/sell agressifs (market orders) sur perp SWAP.
    Mesure la conviction acheteur vs vendeuse en temps réel.
    """
    base = inst_id.split("-")[0]
    data = _get_public(
        "/api/v5/rubik/stat/taker-volume-contract",
        {"instId": f"{base}-USDT-SWAP", "period": period}
    )
    if not data:
        # Fallback SPOT si SWAP indispo
        data = _get_public(
            "/api/v5/rubik/stat/taker-volume",
            {"instId": f"{base}-USDT", "instType": "SPOT", "period": period}
        )
    if not data:
        return {"ratio": None, "score": 0.0, "signal": "Taker N/A"}

    try:
        first = data[0]
        # Format peut être liste [ts, sell, buy] ou dict
        if isinstance(first, list):
            sell_vol = float(first[1])
            buy_vol = float(first[2])
        else:
            buy_vol = float(first.get("buyVol", 0) or 0)
            sell_vol = float(first.get("sellVol", 1) or 1)
        ratio = buy_vol / sell_vol if sell_vol > 0 else 1.0

        if ratio > 1.8:
            score, signal = 0.75, f"Takers {ratio:.2f} - acheteurs dominants"
        elif ratio > 1.3:
            score, signal = 0.4, f"Takers {ratio:.2f} - pression acheteuse"
        elif ratio < 0.55:
            score, signal = -0.75, f"Takers {ratio:.2f} - vendeurs dominants"
        elif ratio < 0.77:
            score, signal = -0.4, f"Takers {ratio:.2f} - pression vendeuse"
        else:
            score, signal = 0.0, f"Takers {ratio:.2f} - équilibré"
        return {"ratio": round(ratio, 3), "score": score, "signal": signal}
    except Exception as e:
        logger.debug(f"Taker {inst_id} : {e}")
        return {"ratio": None, "score": 0.0, "signal": "Taker N/A"}


# ─── Liquidations ─────────────────────────────────────────────────────────────

def get_liquidation_context(inst_id: str) -> dict:
    """Liquidations long/short récentes — proxy pour squeeze potentiel."""
    try:
        liq_long = _get_public(
            "/api/v5/public/liquidation-orders",
            {"instType": "SWAP", "instId": inst_id, "side": "buy",
             "state": "filled", "limit": "20"}
        )
        liq_short = _get_public(
            "/api/v5/public/liquidation-orders",
            {"instType": "SWAP", "instId": inst_id, "side": "sell",
             "state": "filled", "limit": "20"}
        )
        long_n = len(liq_long) if isinstance(liq_long, list) else 0
        short_n = len(liq_short) if isinstance(liq_short, list) else 0

        if long_n > 15:
            score, signal = 0.5, f"Liqs longs : {long_n} - flush potentiel terminé"
        elif short_n > 15:
            score, signal = -0.5, f"Liqs shorts : {short_n} - squeeze possible"
        else:
            score, signal = 0.0, f"Liqs: {long_n}L/{short_n}S"
        return {"long_liqs": long_n, "short_liqs": short_n,
                "score": score, "signal": signal}
    except Exception as e:
        logger.debug(f"Liqs {inst_id} : {e}")
        return {"long_liqs": 0, "short_liqs": 0, "score": 0.0, "signal": "Liqs N/A"}


# ─── Analyse complète ────────────────────────────────────────────────────────

def analyze(inst_id: str) -> dict:
    """
    Microstructure score (hors funding, géré séparément dans funding_score.py).

    Pondération interne :
      L/S ratio    40%
      Taker volume 40%
      Liquidations 20%

    Score final ∈ [-1.5, +1.5], avec verdict + signaux pour log.

    Note : Open Interest n'est pas inclus dans le score (Bot 1 le scorait à 0
    aussi). On le supprime de l'appel pour économiser ~10s/cycle sur 99 paires.
    """
    ls = get_long_short_ratio(inst_id)
    taker = get_taker_volume(inst_id)
    liqs = get_liquidation_context(inst_id)

    score = (ls["score"] * 0.40 + taker["score"] * 0.40 + liqs["score"] * 0.20)
    score = round(max(-1.5, min(1.5, score)), 2)

    signals = [item["signal"] for item in (ls, taker, liqs)
               if item.get("signal") and "N/A" not in item.get("signal", "")]

    if score >= 0.6:
        verdict = "MICROSTRUCTURE HAUSSIERE"
    elif score >= 0.2:
        verdict = "MICROSTRUCTURE LEGEREMENT HAUSSIERE"
    elif score <= -0.6:
        verdict = "MICROSTRUCTURE BAISSIERE"
    elif score <= -0.2:
        verdict = "MICROSTRUCTURE LEGEREMENT BAISSIERE"
    else:
        verdict = "MICROSTRUCTURE NEUTRE"

    return {
        "inst_id": inst_id,
        "score": score,
        "verdict": verdict,
        "signals": signals,
        "long_short": ls,
        "taker_volume": taker,
        "liquidations": liqs,
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    for inst in ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]:
        r = analyze(inst)
        print(f"\n{inst}: score={r['score']:+.2f} | {r['verdict']}")
        for s in r["signals"]:
            print(f"  - {s}")
