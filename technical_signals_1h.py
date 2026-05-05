"""
Signaux techniques minimaux pour TF 1h.

Bot 1 : `technical_signals.py` = 1347 lignes, conçu pour daily, lookback 200 EMA.
Bot 2 : version simplifiée (~250 lignes), indicateurs essentiels pour intraday 1h.

Indicateurs :
  - RSI 14
  - MACD (12, 26, 9)
  - EMA 9, 21, 50 — alignement = direction de tendance
  - Bollinger Bands (20, 2)
  - ATR 14 — pour normaliser la volatilité
  - Détection de breakout BB (close hors BB sur 2 bougies)

Pas d'Ichimoku (signal lent), pas de Fibonacci (subjectif), pas de Supertrend
(redondant avec EMA + ATR), pas de stochastique (RSI suffit).

Score final signé : [-3, +3].
  > 0 = bias long
  < 0 = bias short
  Magnitude = nombre de signaux alignés × intensité
"""

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


# ─── Indicateurs ──────────────────────────────────────────────────────────────

def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    macd_line = ema_f - ema_s
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


def bollinger(close: pd.Series, period: int = 20, std_mult: float = 2.0):
    ma = close.rolling(period).mean()
    sd = close.rolling(period).std()
    upper = ma + sd * std_mult
    lower = ma - sd * std_mult
    return upper, ma, lower


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    tr = pd.concat([
        high - low,
        (high - close.shift()).abs(),
        (low - close.shift()).abs(),
    ], axis=1).max(axis=1)
    return tr.rolling(period).mean()


# ─── Score composite ─────────────────────────────────────────────────────────

def compute_signal(df: pd.DataFrame) -> dict:
    """
    Calcule le score technique signé sur la dernière bougie 1h.

    Returns:
        {
          "score":        float ∈ [-3, +3],
          "verdict":      str,
          "signaux":      list[str],
          "prix_actuel":  float,
          "rsi":          float,
          "atr":          float,
          "ema_9":        float,
          "ema_21":       float,
          "ema_50":       float,
          "bb_upper":     float,
          "bb_lower":     float,
        }
    """
    if df.empty or len(df) < 60:
        return {"score": 0.0, "verdict": "données insuffisantes",
                "signaux": [], "prix_actuel": 0}

    close = df["close"]
    cur = float(close.iloc[-1])

    # Indicateurs
    r = rsi(close)
    macd_line, sig_line, hist = macd(close)
    upper, mid, lower = bollinger(close)
    atr_v = atr(df)
    ema9 = close.ewm(span=9, adjust=False).mean()
    ema21 = close.ewm(span=21, adjust=False).mean()
    ema50 = close.ewm(span=50, adjust=False).mean()

    score = 0.0
    signaux = []

    # ── 1. RSI (max ±0.7) ────────────────────────────────────────────────────
    rsi_now = float(r.iloc[-1])
    rsi_prev = float(r.iloc[-2]) if len(r) > 1 else rsi_now

    if rsi_now < 30 and rsi_prev <= rsi_now:
        score += 0.7
        signaux.append(f"RSI sort de survente ({rsi_now:.0f})")
    elif rsi_now > 70 and rsi_prev >= rsi_now:
        score -= 0.7
        signaux.append(f"RSI sort de surachat ({rsi_now:.0f})")
    elif 40 < rsi_now < 60:
        pass  # zone neutre, pas de signal
    elif rsi_now > 60 and rsi_now < 70:
        score += 0.2
        signaux.append(f"RSI haussier ({rsi_now:.0f})")
    elif rsi_now > 30 and rsi_now < 40:
        score -= 0.2
        signaux.append(f"RSI baissier ({rsi_now:.0f})")

    # ── 2. MACD (max ±0.7) ───────────────────────────────────────────────────
    macd_now = float(macd_line.iloc[-1])
    sig_now = float(sig_line.iloc[-1])
    hist_now = float(hist.iloc[-1])
    hist_prev = float(hist.iloc[-2]) if len(hist) > 1 else hist_now

    if macd_now > sig_now and hist_now > hist_prev > 0:
        score += 0.7
        signaux.append("MACD croisement haussier confirmé")
    elif macd_now < sig_now and hist_now < hist_prev < 0:
        score -= 0.7
        signaux.append("MACD croisement baissier confirmé")
    elif macd_now > sig_now:
        score += 0.3
        signaux.append("MACD au-dessus du signal")
    elif macd_now < sig_now:
        score -= 0.3
        signaux.append("MACD sous le signal")

    # ── 3. Bollinger Bands (max ±0.8) ────────────────────────────────────────
    upper_now = float(upper.iloc[-1])
    lower_now = float(lower.iloc[-1])
    upper_prev = float(upper.iloc[-2]) if len(upper) > 1 else upper_now
    lower_prev = float(lower.iloc[-2]) if len(lower) > 1 else lower_now
    close_prev = float(close.iloc[-2]) if len(close) > 1 else cur

    # Breakout BB confirmé sur 2 bougies
    if cur > upper_now and close_prev > upper_prev:
        score += 0.8
        signaux.append("Breakout BB upper confirmé (2 bougies)")
    elif cur < lower_now and close_prev < lower_prev:
        score -= 0.8
        signaux.append("Breakdown BB lower confirmé (2 bougies)")
    elif cur > upper_now:
        score += 0.4
        signaux.append("Au-dessus de BB upper")
    elif cur < lower_now:
        score -= 0.4
        signaux.append("Sous BB lower")

    # ── 4. EMA alignment (max ±0.8) ──────────────────────────────────────────
    e9 = float(ema9.iloc[-1])
    e21 = float(ema21.iloc[-1])
    e50 = float(ema50.iloc[-1])

    if cur > e9 > e21 > e50:
        score += 0.8
        signaux.append("EMA alignées hausse (cur > 9 > 21 > 50)")
    elif cur < e9 < e21 < e50:
        score -= 0.8
        signaux.append("EMA alignées baisse (cur < 9 < 21 < 50)")
    elif cur > e9 > e21:
        score += 0.4
        signaux.append("EMA 9/21 alignées hausse")
    elif cur < e9 < e21:
        score -= 0.4
        signaux.append("EMA 9/21 alignées baisse")

    # ── Clamp + verdict ──────────────────────────────────────────────────────
    score = round(max(-3.0, min(3.0, score)), 2)

    if score >= 2.0:
        verdict = "FORT SIGNAL LONG"
    elif score >= 1.0:
        verdict = "Signal long modéré"
    elif score <= -2.0:
        verdict = "FORT SIGNAL SHORT"
    elif score <= -1.0:
        verdict = "Signal short modéré"
    else:
        verdict = "Neutre"

    atr_now = float(atr_v.iloc[-1]) if not pd.isna(atr_v.iloc[-1]) else 0

    return {
        "score":       score,
        "verdict":     verdict,
        "signaux":     signaux,
        "prix_actuel": cur,
        "rsi":         round(rsi_now, 1),
        "atr":         round(atr_now, 6),
        "ema_9":       round(e9, 6),
        "ema_21":      round(e21, 6),
        "ema_50":      round(e50, 6),
        "bb_upper":    round(upper_now, 6),
        "bb_lower":    round(lower_now, 6),
    }


def analyze(df: pd.DataFrame) -> dict:
    """Wrapper compatible avec le scanner. Retourne le signal."""
    return {"signal": compute_signal(df)}


if __name__ == "__main__":
    import okx_futures as okx
    logging.basicConfig(level=logging.INFO)

    for inst in ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
                 "PEPE-USDT-SWAP", "DOGE-USDT-SWAP"]:
        df = okx.get_ohlcv(inst, bar="1H", limit=200)
        if df.empty:
            print(f"\n{inst}: pas de données")
            continue
        s = compute_signal(df)
        print(f"\n{inst} @ ${s['prix_actuel']:,.4f}")
        print(f"  score = {s['score']:+.2f} | {s['verdict']}")
        print(f"  RSI={s['rsi']} | ATR=${s['atr']:.4f}")
        for sig in s["signaux"]:
            print(f"  - {sig}")
