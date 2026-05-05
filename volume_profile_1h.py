"""
Volume Profile sur OHLCV 1h — adapté de Bot 1.

POC / VAH / VAL / HVN / LVN calculés sur les ~10 derniers jours de bougies 1h.
240 bougies 1h ≈ 10 jours, suffisant pour identifier les niveaux clés en intraday.

Score : -1.0 à +1.0 (intégré dans le score composite scanner).
"""

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# 240 bougies 1h = 10 jours — fenêtre intraday pertinente
DEFAULT_LOOKBACK_BARS = 240


def compute_volume_profile(df: pd.DataFrame, n_bins: int = 50,
                            lookback_bars: int = DEFAULT_LOOKBACK_BARS) -> dict:
    """
    Volume profile sur les N dernières bougies 1h.
    """
    if df.empty or len(df) < 5:
        return {"error": "données insuffisantes"}

    df_w = df.tail(lookback_bars).copy()
    if df_w.empty:
        return {"error": "fenêtre vide"}

    price_min = float(df_w["low"].min())
    price_max = float(df_w["high"].max())
    if price_min >= price_max:
        return {"error": "range de prix invalide"}

    bins = np.linspace(price_min, price_max, n_bins + 1)
    bin_centers = (bins[:-1] + bins[1:]) / 2
    volume_at_price = np.zeros(n_bins)

    for _, row in df_w.iterrows():
        low, high, vol = row["low"], row["high"], row["volume"]
        if vol <= 0 or pd.isna(vol):
            continue

        low_idx = max(0, min(np.searchsorted(bins, low, side="left"), n_bins - 1))
        high_idx = max(0, min(np.searchsorted(bins, high, side="right"), n_bins))

        n_touched = high_idx - low_idx
        if n_touched <= 0:
            n_touched = 1
            high_idx = low_idx + 1

        volume_at_price[low_idx:high_idx] += vol / n_touched

    if volume_at_price.sum() == 0:
        return {"error": "volume nul"}

    # POC
    poc_idx = int(np.argmax(volume_at_price))
    poc_price = float(bin_centers[poc_idx])

    # Value Area (70%)
    target = volume_at_price.sum() * 0.70
    accumulated = volume_at_price[poc_idx]
    lo, hi = poc_idx, poc_idx
    while accumulated < target:
        up_ok = hi + 1 < n_bins
        dn_ok = lo - 1 >= 0
        if up_ok and dn_ok:
            if volume_at_price[hi + 1] >= volume_at_price[lo - 1]:
                hi += 1; accumulated += volume_at_price[hi]
            else:
                lo -= 1; accumulated += volume_at_price[lo]
        elif up_ok:
            hi += 1; accumulated += volume_at_price[hi]
        elif dn_ok:
            lo -= 1; accumulated += volume_at_price[lo]
        else:
            break

    vah = float(bin_centers[hi])
    val = float(bin_centers[lo])

    vol_mean = volume_at_price.mean()
    vol_std = volume_at_price.std()
    hvn = [float(bin_centers[i]) for i in range(n_bins)
           if volume_at_price[i] > vol_mean + vol_std]

    current = float(df["close"].iloc[-1])
    in_va = val <= current <= vah
    above_poc = current > poc_price
    dist_poc = (current - poc_price) / poc_price * 100

    supports = sorted([p for p in hvn if p < current], reverse=True)[:3]
    resistances = sorted([p for p in hvn if p > current])[:3]

    return {
        "poc": round(poc_price, 6),
        "vah": round(vah, 6),
        "val": round(val, 6),
        "current_price": round(current, 6),
        "in_value_area": in_va,
        "above_poc": above_poc,
        "distance_poc_pct": round(dist_poc, 2),
        "hvn_supports": [round(p, 6) for p in supports],
        "hvn_resistances": [round(p, 6) for p in resistances],
        "lookback_bars": lookback_bars,
    }


def analyze(df: pd.DataFrame) -> dict:
    """Score VP pour le scanner. Score ∈ [-1.0, +1.0]."""
    if df.empty:
        return {"score": 0.0, "verdict": "VP indisponible", "signals": []}

    vp = compute_volume_profile(df)
    if "error" in vp:
        return {"score": 0.0, "verdict": "VP indisponible", "signals": []}

    score = 0.0
    signals = []
    current = vp["current_price"]
    poc, vah, val = vp["poc"], vp["vah"], vp["val"]

    # Position dans / hors Value Area
    if vp["in_value_area"]:
        if vp["above_poc"]:
            score += 0.3
            signals.append(f"Au-dessus du POC ${poc:.4f} dans la Value Area")
        else:
            score -= 0.2
            signals.append(f"Sous le POC ${poc:.4f} dans la Value Area")
    else:
        if current > vah:
            score += 0.5
            signals.append(f"Breakout au-dessus de VAH ${vah:.4f}")
        elif current < val:
            score -= 0.5
            signals.append(f"Breakdown sous VAL ${val:.4f}")

    # Supports / résistances proches
    if vp["hvn_supports"]:
        nearest = vp["hvn_supports"][0]
        d = (current - nearest) / current * 100
        if d < 2:
            score += 0.2
            signals.append(f"Support HVN proche ${nearest:.4f} ({d:.1f}%)")
    if vp["hvn_resistances"]:
        nearest = vp["hvn_resistances"][0]
        d = (nearest - current) / current * 100
        if d < 2:
            score -= 0.2
            signals.append(f"Résistance HVN proche ${nearest:.4f} ({d:.1f}%)")

    score = round(max(-1.0, min(1.0, score)), 2)
    verdict = ("VP bullish" if score > 0.3 else
               "VP bearish" if score < -0.3 else "VP neutre")

    return {
        "score": score, "verdict": verdict, "signals": signals,
        "poc": poc, "vah": vah, "val": val,
        "in_value_area": vp["in_value_area"],
        "hvn_supports": vp["hvn_supports"],
        "hvn_resistances": vp["hvn_resistances"],
    }


if __name__ == "__main__":
    import okx_futures as okx
    logging.basicConfig(level=logging.INFO)

    for inst in ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]:
        df = okx.get_ohlcv(inst, bar="1H", limit=240)
        r = analyze(df)
        print(f"\n{inst}: score={r['score']:+.2f} | POC=${r.get('poc', 0):.4f} | "
              f"VAH=${r.get('vah', 0):.4f} | VAL=${r.get('val', 0):.4f}")
        for s in r["signals"]:
            print(f"  - {s}")
