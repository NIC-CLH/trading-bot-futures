"""
Client OKX Futures (Perpetual Swaps) — mode démo (paper trading).

Bot 2 utilise UNIQUEMENT le compte démo OKX.
Header `x-simulated-trading: 1` envoyé sur toutes les requêtes (auth + public).
Domaine : www.okx.com (le mode démo n'utilise pas l'endpoint EEA).

Lecture seule pour la phase 0 :
  - get_swap_pairs()      : univers perps USDT-margined
  - get_ohlcv()           : bougies 15m/1H/4H/1D
  - get_ticker()          : prix actuel + bid/ask + volume
  - get_funding_rate()    : funding courant + prochain
  - get_demo_balance()    : solde du compte démo (auth)

Les ordres seront ajoutés en phase 1 (paper_executor).
"""

import base64
import hashlib
import hmac
import json
import logging
import os
from datetime import datetime

import pandas as pd
import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

OKX_BASE = "https://eea.okx.com"  # compte démo OKX EEA (même domaine que prod EEA)
API_KEY = os.getenv("DEMO_OKX_API_KEY", "")
SECRET = os.getenv("DEMO_OKX_SECRET", "")
PASSPHRASE = os.getenv("DEMO_OKX_PASSPHRASE", "")


# ─── Authentification ─────────────────────────────────────────────────────────

def _sign(timestamp: str, method: str, path: str, body: str = "") -> str:
    msg = f"{timestamp}{method.upper()}{path}{body}"
    return base64.b64encode(
        hmac.new(SECRET.encode(), msg.encode(), hashlib.sha256).digest()
    ).decode()


def _headers(method: str, path: str, body: str = "", auth: bool = False) -> dict:
    """
    Construit les headers HTTP. Le header x-simulated-trading=1 est
    OBLIGATOIRE en mode démo, même pour les endpoints publics.
    """
    base = {
        "Content-Type": "application/json",
        "x-simulated-trading": "1",
    }
    if not auth:
        return base
    ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return {
        **base,
        "OK-ACCESS-KEY": API_KEY,
        "OK-ACCESS-SIGN": _sign(ts, method, path, body),
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": PASSPHRASE,
    }


def _get(path: str, params: dict | None = None, auth: bool = False) -> list:
    if params:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        signed_path = f"{path}?{query}"
    else:
        signed_path = path

    url = OKX_BASE + signed_path
    resp = requests.get(url, headers=_headers("GET", signed_path, auth=auth), timeout=10)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "0":
        raise Exception(f"OKX error {data.get('code')} : {data.get('msg')}")
    return data.get("data", [])


# ─── Univers SWAP ─────────────────────────────────────────────────────────────

def get_swap_pairs(min_volume_usdt: float = 500_000) -> list[str]:
    """
    Retourne tous les perps OKX (SWAP USDT-margined) actifs avec volume 24h
    >= seuil, triés par volume décroissant.

    Format des instId : 'BTC-USDT-SWAP', 'ETH-USDT-SWAP', etc.
    """
    try:
        instruments = _get("/api/v5/public/instruments", {"instType": "SWAP"})
        # USDT-margined uniquement (pas USDC swap, pas crypto-margined)
        usdt_pairs = [
            d["instId"] for d in instruments
            if d.get("settleCcy") == "USDT" and d.get("state") == "live"
        ]

        tickers = _get("/api/v5/market/tickers", {"instType": "SWAP"})
        # volCcy24h sur SWAP = volume en USDT (devise de règlement)
        volumes = {t["instId"]: float(t.get("volCcy24h", 0) or 0) for t in tickers}

        filtered = sorted(
            [p for p in usdt_pairs if volumes.get(p, 0) >= min_volume_usdt],
            key=lambda p: volumes.get(p, 0),
            reverse=True,
        )

        logger.info(
            f"OKX SWAP : {len(usdt_pairs)} perps USDT live | "
            f"{len(filtered)} avec vol > ${min_volume_usdt/1e6:.1f}M/24h"
        )
        return filtered
    except Exception as e:
        logger.error(f"get_swap_pairs : {e}")
        return []


# ─── OHLCV ────────────────────────────────────────────────────────────────────

def get_ohlcv(inst_id: str, bar: str = "1H", limit: int = 200) -> pd.DataFrame:
    """
    Bougies historiques OKX SWAP.

    Args:
        inst_id : ex 'BTC-USDT-SWAP'
        bar     : '1m','5m','15m','30m','1H','2H','4H','6H','12H','1D','1W','1M'
        limit   : 1-300 (OKX max 300 par requête)

    Retourne un DataFrame indexé par timestamp (croissant), colonnes :
        open, high, low, close, volume
    """
    try:
        data = _get("/api/v5/market/candles", {
            "instId": inst_id,
            "bar": bar,
            "limit": min(limit, 300),
        })
        if not data:
            return pd.DataFrame()

        rows = [{
            "timestamp": pd.to_datetime(int(c[0]), unit="ms"),
            "open":   float(c[1]),
            "high":   float(c[2]),
            "low":    float(c[3]),
            "close":  float(c[4]),
            "volume": float(c[5]),
        } for c in data]

        df = pd.DataFrame(rows).set_index("timestamp").sort_index()
        return df
    except Exception as e:
        logger.warning(f"OHLCV {inst_id} {bar} : {e}")
        return pd.DataFrame()


# ─── Ticker ───────────────────────────────────────────────────────────────────

def get_ticker(inst_id: str) -> dict:
    """Prix actuel + meilleur bid/ask + volume 24h."""
    try:
        data = _get("/api/v5/market/ticker", {"instId": inst_id})
        if not data:
            return {}
        d = data[0]
        return {
            "last":         float(d.get("last", 0)),
            "ask":          float(d.get("askPx", 0) or 0),
            "bid":          float(d.get("bidPx", 0) or 0),
            "vol_24h_usdt": float(d.get("volCcy24h", 0) or 0),
        }
    except Exception as e:
        logger.warning(f"ticker {inst_id} : {e}")
        return {}


# ─── Funding rate ─────────────────────────────────────────────────────────────

def get_funding_rate(inst_id: str) -> dict:
    """
    Funding rate courant + prochain. Sur OKX, le funding est versé toutes les 8h
    aux UTC 00:00, 08:00, 16:00.

    Retourne :
        current   : taux courant (ex 0.0001 = 0.01%)
        next      : taux estimé pour le prochain cycle
        next_time : timestamp ms du prochain versement
    """
    try:
        data = _get("/api/v5/public/funding-rate", {"instId": inst_id})
        if not data:
            return {}
        d = data[0]
        return {
            "current":   float(d.get("fundingRate", 0) or 0),
            "next":      float(d.get("nextFundingRate", 0) or 0),
            "next_time": int(d.get("nextFundingTime", 0) or 0),
        }
    except Exception as e:
        logger.warning(f"funding {inst_id} : {e}")
        return {}


# ─── Compte démo (auth) ───────────────────────────────────────────────────────

def get_demo_balance() -> dict[str, float]:
    """
    Solde du compte démo OKX. Le compte démo OKX commence à 100 000 USDT.

    Retourne {ccy: cashBal} — ex {'USDT': 100000.0}
    """
    try:
        data = _get("/api/v5/account/balance", auth=True)
        balances = {}
        for account in data:
            for detail in account.get("details", []):
                qty = float(detail.get("cashBal", 0) or 0)
                if qty > 0:
                    balances[detail["ccy"]] = qty
        return balances
    except Exception as e:
        logger.error(f"balance démo OKX : {e}")
        return {}


# ─── Smoke test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    print("=" * 60)
    print("  OKX Futures client (mode démo) — smoke test")
    print("=" * 60)

    # 1. Univers
    pairs = get_swap_pairs(min_volume_usdt=500_000)
    print(f"\n[1] Perps liquides : {len(pairs)}")
    if pairs:
        print(f"    Top 10 par volume : {pairs[:10]}")

    # 2. OHLCV BTC 1H
    df = get_ohlcv("BTC-USDT-SWAP", bar="1H", limit=10)
    print(f"\n[2] BTC-USDT-SWAP 1H ({len(df)} bougies)")
    if not df.empty:
        print(df.tail(5).to_string())

    # 3. Funding rate BTC
    fund = get_funding_rate("BTC-USDT-SWAP")
    print(f"\n[3] BTC funding : current={fund.get('current', 0):.4%} | "
          f"next={fund.get('next', 0):.4%}")

    # 4. Ticker BTC
    tk = get_ticker("BTC-USDT-SWAP")
    print(f"\n[4] BTC ticker : last=${tk.get('last', 0):,.1f} | "
          f"bid=${tk.get('bid', 0):,.1f} | ask=${tk.get('ask', 0):,.1f}")

    # 5. Compte démo (auth)
    bal = get_demo_balance()
    print(f"\n[5] Balance compte démo : {bal}")

    print("\n" + "=" * 60)
