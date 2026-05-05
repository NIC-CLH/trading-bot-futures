"""
Détecteur de régime de marché — TF 1h pour Bot 2.

3 approches combinées (héritées Bot 1) :
  1. HMM (Hidden Markov Model) — régime caché bull/bear/sideways
  2. GARCH(1,1) — volatilité future estimée
  3. Ruptures — détection de changements structurels

Adaptations vs Bot 1 :
  - Plus de bougies recommandées (200-500 1h vs 90 daily)
  - Volatilité annualisée recalibrée pour TF 1h (× sqrt(365×24) au lieu de × sqrt(365))
  - Fallbacks toujours présents si hmmlearn/arch/ruptures absents

Score retourné est utilisé par le scanner pour AJUSTER LE SEUIL D'ENTRÉE
(pas comme composante du score lui-même, voir CLAUDE.md).
"""

import logging
import warnings

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
logger = logging.getLogger(__name__)

REGIME_BULL = "bull"
REGIME_BEAR = "bear"
REGIME_SIDEWAYS = "sideways"


# ─── HMM ──────────────────────────────────────────────────────────────────────

def detect_regime_hmm(df: pd.DataFrame, n_states: int = 3) -> dict:
    try:
        from hmmlearn.hmm import GaussianHMM

        if df.empty or len(df) < 100:
            return {"regime": REGIME_SIDEWAYS, "confidence": 0.5, "source": "insufficient_data"}

        returns = df["close"].pct_change().dropna()
        vol = returns.rolling(12).std().dropna()  # 12 bougies 1h ≈ demi-jour
        n = min(len(returns), len(vol))
        returns = returns.iloc[-n:]
        vol = vol.iloc[-n:]

        X = np.column_stack([returns.values, vol.values])
        model = GaussianHMM(n_components=n_states, covariance_type="full",
                            n_iter=200, random_state=42)
        model.fit(X)

        states = model.predict(X)
        current = states[-1]
        conf = float(model.predict_proba(X)[-1][current])

        means = []
        for s in range(n_states):
            mask = states == s
            means.append((s, float(returns.values[mask].mean()) if mask.sum() > 0 else 0.0))
        sorted_states = sorted(means, key=lambda x: x[1])
        bear_s = sorted_states[0][0]
        bull_s = sorted_states[-1][0]

        regime = (REGIME_BULL if current == bull_s else
                  REGIME_BEAR if current == bear_s else REGIME_SIDEWAYS)

        return {"regime": regime, "confidence": round(conf, 3), "source": "hmm"}

    except ImportError:
        return _regime_fallback(df)
    except Exception as e:
        logger.warning(f"HMM erreur : {e} — fallback")
        return _regime_fallback(df)


def _regime_fallback(df: pd.DataFrame) -> dict:
    """EMA-based fallback si hmmlearn manque."""
    if df.empty or len(df) < 50:
        return {"regime": REGIME_SIDEWAYS, "confidence": 0.5, "source": "fallback"}
    close = df["close"]
    ema20 = close.ewm(span=20).mean().iloc[-1]
    ema50 = close.ewm(span=50).mean().iloc[-1]
    cur = close.iloc[-1]
    if cur > ema20 > ema50:
        return {"regime": REGIME_BULL, "confidence": 0.7, "source": "ema_fallback"}
    if cur < ema20 < ema50:
        return {"regime": REGIME_BEAR, "confidence": 0.7, "source": "ema_fallback"}
    return {"regime": REGIME_SIDEWAYS, "confidence": 0.6, "source": "ema_fallback"}


# ─── GARCH ────────────────────────────────────────────────────────────────────

def estimate_volatility_garch(df: pd.DataFrame) -> dict:
    """Volatilité future GARCH(1,1) sur returns 1h, annualisée."""
    try:
        from arch import arch_model

        if df.empty or len(df) < 100:
            return _vol_fallback(df)

        # Pour 1h : annualisation via sqrt(365 × 24)
        returns = df["close"].pct_change().dropna() * 100
        am = arch_model(returns, vol="Garch", p=1, q=1, dist="normal")
        res = am.fit(disp="off", show_warning=False)
        forecast = res.forecast(horizon=1)
        vol_1h = float(np.sqrt(forecast.variance.iloc[-1, 0]))
        vol_ann = vol_1h * np.sqrt(365 * 24)

        return _classify_vol(vol_ann, vol_1h, source="garch")

    except ImportError:
        return _vol_fallback(df)
    except Exception as e:
        logger.warning(f"GARCH erreur : {e} — fallback")
        return _vol_fallback(df)


def _vol_fallback(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"vol_annualized": 80.0, "position_multiplier": 0.7,
                "vol_regime": "normal", "source": "fallback"}
    returns = df["close"].pct_change().dropna()
    vol_1h_pct = float(returns.std() * 100)
    vol_ann = vol_1h_pct * np.sqrt(365 * 24)
    return _classify_vol(vol_ann, vol_1h_pct, source="std_fallback")


def _classify_vol(vol_ann: float, vol_period: float, source: str) -> dict:
    if vol_ann < 40:
        regime, mult = "calm", 1.0
    elif vol_ann < 80:
        regime, mult = "normal", 0.85
    elif vol_ann < 120:
        regime, mult = "elevated", 0.65
    else:
        regime, mult = "extreme", 0.40
    return {
        "vol_annualized": round(vol_ann, 1),
        "vol_period_pct": round(vol_period, 3),
        "vol_regime": regime,
        "position_multiplier": mult,
        "source": source,
    }


# ─── Ruptures ─────────────────────────────────────────────────────────────────

def detect_changepoints(df: pd.DataFrame) -> dict:
    try:
        import ruptures as rpt

        if df.empty or len(df) < 50:
            return {"n_changepoints_recent": 0, "bars_since_last": 999, "recent_break": False}

        signal = df["close"].values
        algo = rpt.Pelt(model="rbf", min_size=3, jump=1).fit(signal)
        breakpoints = algo.predict(pen=3)

        n = len(df)
        # 24 = 1 jour en 1h ; on regarde les 7 derniers jours
        recent = [b for b in breakpoints if b >= n - (24 * 7) and b < n]
        bars_since = (n - recent[-1]) if recent else 999

        return {
            "n_changepoints_recent": len(recent),
            "bars_since_last": bars_since,
            "recent_break": bars_since <= 24,  # rupture dans les dernières 24h
        }
    except ImportError:
        return {"n_changepoints_recent": 0, "bars_since_last": 999, "recent_break": False}
    except Exception as e:
        logger.warning(f"Ruptures erreur : {e}")
        return {"n_changepoints_recent": 0, "bars_since_last": 999, "recent_break": False}


# ─── Analyse combinée ────────────────────────────────────────────────────────

def analyze(df: pd.DataFrame) -> dict:
    hmm = detect_regime_hmm(df)
    garch = estimate_volatility_garch(df)
    breaks = detect_changepoints(df)

    regime = hmm.get("regime", REGIME_SIDEWAYS)
    vol_regime = garch.get("vol_regime", "normal")
    pos_mult = garch.get("position_multiplier", 0.85)
    recent_break = breaks.get("recent_break", False)

    score = 0.0
    signals = []
    if regime == REGIME_BULL:
        score += 0.6; signals.append(f"Régime BULL (HMM {hmm.get('confidence', 0):.0%})")
    elif regime == REGIME_BEAR:
        score -= 0.6; signals.append(f"Régime BEAR (HMM {hmm.get('confidence', 0):.0%})")
    else:
        signals.append("Régime SIDEWAYS")

    if vol_regime == "extreme":
        score *= 0.5
        pos_mult = min(pos_mult, 0.4)
        signals.append(f"Vol EXTREME ({garch.get('vol_annualized', 0):.0f}%/an)")
    elif vol_regime == "elevated":
        score *= 0.75
        signals.append(f"Vol élevée ({garch.get('vol_annualized', 0):.0f}%/an)")

    if recent_break:
        score *= 0.7
        pos_mult = min(pos_mult, 0.6)
        signals.append(f"Rupture structurelle il y a {breaks['bars_since_last']}h")

    score = round(max(-1.0, min(1.0, score)), 2)

    return {
        "regime": regime,
        "score": score,
        "position_multiplier": round(pos_mult, 2),
        "vol_annualized": garch.get("vol_annualized", 80),
        "vol_regime": vol_regime,
        "hmm_confidence": hmm.get("confidence", 0.5),
        "recent_break": recent_break,
        "signals": signals,
        "verdict": f"{regime} | vol {vol_regime}",
    }


if __name__ == "__main__":
    import okx_futures as okx
    logging.basicConfig(level=logging.INFO)
    for inst in ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP"]:
        df = okx.get_ohlcv(inst, bar="1H", limit=300)
        r = analyze(df)
        print(f"\n{inst}: regime={r['regime']:9} | score={r['score']:+.2f} | "
              f"vol_ann={r['vol_annualized']:.0f}% | mult={r['position_multiplier']}")
        for s in r["signals"]:
            print(f"  - {s}")
