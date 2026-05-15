"""
Configuration globale Bot 2 — Futures Swing Trading.

Bot 2 = clone Bot 1 adapté au court terme sur perps OKX.
Différences clés :
  - Levier 2x (3x sur setups A+)
  - TP +4% / SL -3% / time stop 24h (vs +12% / ATR / 7j pour Bot 1)
  - Cycle 1h (vs 4h)
  - Long ET short
  - Mode paper trading uniquement (compte démo OKX 100k USDT virtuels)
"""

# ── Stratégie ────────────────────────────────────────────────────────────────
PAIR_FILTER_MIN_VOLUME = 500_000      # USDT volume 24h minimum (élimine illiquides)

# Seuil de score MINIMUM par bucket pour déclencher une entrée.
# Backtest 90j × 20 paires (2317 trades baseline) : aucune config profitable
# sauf MAJORS LONG only avec seuil 1.6 et R/R 3:1 (PF 1.76, WR 50%, P&L +49%).
# Conclusion : edge réel UNIQUEMENT sur les majors, en LONG, avec seuil strict.
# Midcap/memes désactivés via ALLOWED_BUCKETS ci-dessous.
SCORE_MIN_MAJORS = 1.6  # 87% du max théorique 1.83 — strict, qualité prouvée backtest
SCORE_MIN_MIDCAP = 1.2  # inchangé mais inactif (bucket désactivé)
SCORE_MIN_MEMES  = 1.3  # inchangé mais inactif (bucket désactivé)
SCORE_MIN = 1.6  # fallback

# Filtres durs (issus du backtest 90j) :
# - LONG only : shorts ont 32% WR partout → perdants systématiques
# - Majors only : midcap/memes ont PF < 0.8, pas d'edge
ALLOWED_BUCKETS = {"majors"}  # ex: {"majors", "midcap"} pour élargir plus tard
ALLOWED_SIDES = {"long"}      # ex: {"long", "short"} pour réactiver shorts

LEVIER_DEFAUT = 2                      # levier standard
LEVIER_MAX = 3                         # autorisé sur setups A+
SCORE_A_PLUS = 2.5                     # seuil score pour autoriser 3x

# ── Position management ──────────────────────────────────────────────────────
# R/R 3:1 (validé backtest 90j sur majors LONG) — break-even WR = 25%
# Au WR 50% mesuré, marge confortable : expectancy +1.16% / trade sur marge.
TP_PCT = 0.09                          # +9% take profit (sur prix sous-jacent)
SL_PCT = 0.03                          # -3% stop loss (sur prix sous-jacent)
MAX_HOLDING_HOURS = 24                 # time stop : sortie forcée après 24h
MAINTENANCE_MARGIN_PCT = 0.005         # OKX BTC perp ~0.5% (pour calcul liquidation)

# ── Cycle ────────────────────────────────────────────────────────────────────
CYCLE_HOURS = 1                        # boucle horaire (vs 4h Bot 1)
TF_BIAS = "1H"                         # tendance directionnelle
TF_ENTRY = "15m"                       # timing d'entrée plus fin

# ── Filtre BTC MA50 directionnel (partiel) ────────────────────────────────────
# Bilan 34 trades : 7 shorts perdants sur 8 (12% WR) en bull market.
# Solution : activer le filtre mais en mode partiel via btc_regime_filter :
#   - BTC very bull (deviation >= +3% MA50_4h) → shorts bloqués
#   - BTC very bear (deviation <= -3%)         → longs bloqués
#   - Entre ±3%                                  → both autorisés
# Le scoring (1.4/1.2/1.3) reste le filtre primaire de qualité.
ENABLE_BTC_DIRECTIONAL_FILTER = True
BTC_MA_FILTER_INVERSE = True  # legacy

# ── Mode ─────────────────────────────────────────────────────────────────────
PAPER_MODE = True                      # simulation sur compte démo OKX
RUFLO_NAMESPACE = "bot2"               # mémoire vectorielle isolée du Bot 1

# ── Capital (paper) ──────────────────────────────────────────────────────────
CAPITAL_INITIAL_VIRTUEL = 250.0        # EUR — simule le budget réel cible
TRADES_AVANT_LIVE = 30                 # validation : 30 paper trades min avant live

# ── Coûts simulés (réalistes) ────────────────────────────────────────────────
FEES_TAKER_PCT = 0.0005                # OKX taker = 0.05% par côté
SLIPPAGE_PCT = 0.0005                  # estimation BTC perp (très liquide)

# ── Univers ──────────────────────────────────────────────────────────────────
# Stablecoins / wrapped — exclus du scan (pas de signal price action sur eux)
EXCLUDE_BASE = {
    "USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD", "USDP",
    "WBTC", "WETH", "STETH", "BETH", "BBTC",
}

# ── Persistance ──────────────────────────────────────────────────────────────
DB_PATH = "data/paper_trades.db"
LEDGER_JSON = "data/paper_ledger.json"
