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
SCORE_MIN = 1.7                        # seuil pour déclencher entrée (Bot 1 = 1.5)
LEVIER_DEFAUT = 2                      # levier standard
LEVIER_MAX = 3                         # autorisé sur setups A+
SCORE_A_PLUS = 2.5                     # seuil score pour autoriser 3x

# ── Position management ──────────────────────────────────────────────────────
TP_PCT = 0.04                          # +4% take profit (sur prix sous-jacent)
SL_PCT = 0.03                          # -3% stop loss (sur prix sous-jacent)
MAX_HOLDING_HOURS = 24                 # time stop : sortie forcée après 24h
MAINTENANCE_MARGIN_PCT = 0.005         # OKX BTC perp ~0.5% (pour calcul liquidation)

# ── Cycle ────────────────────────────────────────────────────────────────────
CYCLE_HOURS = 1                        # boucle horaire (vs 4h Bot 1)
TF_BIAS = "1H"                         # tendance directionnelle
TF_ENTRY = "15m"                       # timing d'entrée plus fin

# ── Filtre BTC MA50 (inversé pour shorts) ────────────────────────────────────
# BTC > MA50 (haussier) → longs autorisés, shorts plus durs à valider
# BTC < MA50 (baissier) → longs bloqués, shorts autorisés
BTC_MA_FILTER_INVERSE = True

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
