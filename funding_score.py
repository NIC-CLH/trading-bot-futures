"""
7e dimension du score composite : funding rate (perp specifique).

Logique contrarian :
  - Funding rate très POSITIF (longs payent) = trop de longs sur le marché
    → squeeze à la baisse probable → biais SHORT → score négatif
  - Funding rate très NEGATIF (shorts payent) = trop de shorts
    → squeeze à la hausse probable → biais LONG → score positif

Convention de signe : alignée avec le score composite global.
  score positif  = signal long
  score négatif  = signal short

Seuils calibrés sur OKX (funding versé toutes les 8h, taux moyens
proches de 0 ± 0.01%, mais peuvent atteindre ±0.3% en surchauffe) :

  |rate| ≥ 0.10% (8h)  → magnitude 1.0   (extrême)
  |rate| ≥ 0.05%       → magnitude 0.5   (élevé)
  |rate| ≥ 0.02%       → magnitude 0.2   (modéré)
  sinon                → 0               (neutre)

Bot 1 lit la donnée funding mais ne l'utilise pas pour le scoring d'entrée.
Bot 2 l'intègre comme 7e dimension avec poids 5–10% selon le bucket.
"""

import logging

import okx_futures as okx

logger = logging.getLogger(__name__)


# ─── Conversion rate → score ──────────────────────────────────────────────────

def score_funding(funding_rate: float) -> tuple[float, str]:
    """
    Convertit un funding rate (décimal, ex 0.0001 = 0.01%/8h) en score signé.

    Returns:
        (score, label)  — score ∈ [-1.0, +1.0]
    """
    rate_pct = funding_rate * 100  # en %
    abs_rate = abs(rate_pct)

    if abs_rate >= 0.10:
        magnitude = 1.0
        intensity = "extrême"
    elif abs_rate >= 0.05:
        magnitude = 0.5
        intensity = "élevé"
    elif abs_rate >= 0.02:
        magnitude = 0.2
        intensity = "modéré"
    else:
        return 0.0, f"funding {rate_pct:+.3f}% (neutre)"

    # Funding > 0 -> biais short (contrarian) -> score négatif
    score = -magnitude if funding_rate > 0 else magnitude
    direction = "-> contrarian SHORT" if funding_rate > 0 else "-> contrarian LONG"
    label = f"funding {rate_pct:+.3f}% ({intensity}) {direction}"
    return score, label


# ─── API publique ─────────────────────────────────────────────────────────────

def get_funding_score(inst_id: str) -> dict:
    """
    Pour une paire perp, récupère le funding courant et calcule le score.

    Returns:
        {
          "inst_id":    str,
          "rate":       float,         # décimal (0.0001 = 0.01%)
          "rate_pct":   float,         # en %
          "score":      float,         # ∈ [-1.0, +1.0]
          "label":      str,
          "next_rate":  float | None,
        }
    """
    fund = okx.get_funding_rate(inst_id)
    if not fund:
        return {
            "inst_id": inst_id, "rate": 0.0, "rate_pct": 0.0,
            "score": 0.0, "label": "funding indisponible", "next_rate": None,
        }

    rate = fund.get("current", 0.0) or 0.0
    next_rate = fund.get("next", 0.0) or 0.0
    score, label = score_funding(rate)
    return {
        "inst_id":   inst_id,
        "rate":      rate,
        "rate_pct":  round(rate * 100, 4),
        "score":     score,
        "label":     label,
        "next_rate": next_rate,
    }


# ─── Smoke test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    print("=" * 60)
    print("  funding_score.py — 7e dimension du score composite")
    print("=" * 60)

    # 1. Test logique pure (rates simulés)
    print("\n[1] Logique pure (rates simulés) :")
    for r in [0.0, 0.0001, 0.0003, 0.0005, 0.0010, 0.0030, -0.0005, -0.0015]:
        s, lbl = score_funding(r)
        print(f"    rate {r * 100:+.4f}% -> score {s:+.2f} | {lbl}")

    # 2. Test sur paires réelles OKX
    print("\n[2] Sur 8 perps réels OKX :")
    test_pairs = [
        "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
        "DOGE-USDT-SWAP", "XRP-USDT-SWAP",
        "PEPE-USDT-SWAP", "FLOKI-USDT-SWAP", "SHIB-USDT-SWAP",
    ]
    for inst in test_pairs:
        r = get_funding_score(inst)
        print(f"    {inst:25} | rate {r['rate_pct']:+.4f}% | "
              f"score {r['score']:+.2f} | {r['label']}")

    print("\n" + "=" * 60)
