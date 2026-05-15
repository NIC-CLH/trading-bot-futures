"""
Scanner Bot 2 — orchestrateur des 7 dimensions de score.

Pour chaque paire perp OKX :
  1. Classifie la paire (majors / midcap / memes / exclude)
  2. Récupère OHLCV 1h (200 bougies)
  3. Calcule les 7 dimensions :
       - Technique         (technical_signals_1h)
       - Microstructure    (microstructure_1h, hors funding)
       - Funding           (funding_score)
       - News              (gate binaire — TODO Phase 3)
       - Régime            (regime_detector_1h, → ajuste seuil pas score)
       - Volume Profile    (volume_profile_1h, désactivé pour midcap+memes)
       - Macro             (gate binaire — TODO Phase 3)
  4. Combine avec pondérations du bucket
  5. Applique les gates binaires news + macro (multiplicateur −1/0/+1)
  6. Vérifie le filtre BTC régime (autorise long ou short)
  7. Détermine le seuil d'entrée selon le régime du ticker
  8. Si |score| >= seuil → SIGNAL actionnable

Output : liste de signaux triés par |score| décroissant.

Le scanner ne place AUCUN ordre — c'est run_once_futures qui passe ensuite
chaque signal à paper_executor.open_position().
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import asset_buckets
import btc_regime_filter
import config_futures as cfg
import funding_score
import microstructure_1h as micro
import okx_futures as okx
import regime_detector_1h as regime
import technical_signals_1h as tech
import volume_profile_1h as vp

# Workers parallèles pour le scan : OKX rate limit ~20 req/s, on en utilise ~10
# pour rester confortable. 99 paires × 7 calls / 10 workers = ~70 calls/worker
# en série, ~3-5s/worker → cycle complet ~30-60s (vs 6-7min en séquentiel).
SCAN_WORKERS = 10

logger = logging.getLogger(__name__)


# ─── Seuil d'entrée par bucket + régime ──────────────────────────────────────

def bucket_base_threshold(bucket: str) -> float:
    """
    Seuil minimum par bucket. Reflète le scoring max théorique différent par
    classe d'actif (cf. config_futures pour la justification).
    """
    return {
        "majors": cfg.SCORE_MIN_MAJORS,
        "midcap": cfg.SCORE_MIN_MIDCAP,
        "memes":  cfg.SCORE_MIN_MEMES,
    }.get(bucket, cfg.SCORE_MIN)


def effective_threshold(bucket: str, ticker_regime: str) -> float:
    """
    Seuil final = base du bucket + ajustement régime du ticker.
      Trending (bull/bear) → on accepte le seuil de base
      Sideways             → +0.2 (durcir un peu, signaux moins fiables)
    """
    base = bucket_base_threshold(bucket)
    if ticker_regime == "sideways":
        return base + 0.2
    return base


# ─── Gates binaires news + macro (placeholders pour Phase 3) ─────────────────

def get_news_gate(ticker: str, bucket_features: dict) -> int:
    """
    Retourne un multiplicateur ∈ {-1, 0, +1} appliqué au score.
      +1 : news positive significative (boost)
       0 : neutre / désactivé pour ce bucket
      -1 : news négative bloquante (hack, enquête, suspension)

    Phase 2a : retourne toujours 0 (placeholder).
    Phase 3 : intégrera news_interpreter de Bot 1.
    """
    if not bucket_features.get("use_news_gate", False):
        return 0
    return 0  # TODO: appeler news_interpreter en Phase 3


def get_macro_gate(bucket_features: dict) -> int:
    """
    Macro globale (DXY, Fear & Greed, BTC dominance) : gate on/off.
      +1 : conditions macro favorables
       0 : neutre / désactivé pour memes
      -1 : conditions macro défavorables (forte hausse DXY, panique extrême)

    Phase 2a : retourne toujours 0 (placeholder).
    Phase 3 : intégrera macro_context de Bot 1.
    """
    if not bucket_features.get("use_macro_gate", False):
        return 0
    return 0  # TODO: appeler macro_context en Phase 3


def apply_gates(score: float, news_gate: int, macro_gate: int) -> float:
    """
    Les gates appliquent un multiplicateur additif sur le score, pas une
    composante. Pourquoi : un score faible avec macro favorable ne devient
    pas magiquement actionnable, mais un score décent avec news bloquante
    doit être annulé.

    Règle :
      news_gate == -1  → score *= 0  (bloque entièrement)
      macro_gate == -1 → score *= 0.6 (réduit fortement)
      Les +1 ne boostent PAS (asymétrie volontaire — défensif > opportuniste).
    """
    if news_gate == -1:
        return 0.0
    if macro_gate == -1:
        return score * 0.6
    return score


# ─── Pipeline d'analyse pour 1 paire ─────────────────────────────────────────

def analyze_pair(inst_id: str, btc_regime: dict | None = None) -> dict | None:
    """
    Analyse complète d'une paire et retourne le payload de signal.

    Returns None si le ticker doit être ignoré (bucket exclude, OHLCV vide,
    ou direction interdite par btc_regime_filter).
    """
    bucket = asset_buckets.classify(inst_id)
    if bucket == "exclude":
        return None
    # Backtest 90j a montré : midcap PF 0.75, memes PF 0.7 — pas d'edge.
    # Garder uniquement les buckets validés (majors par défaut).
    if bucket not in cfg.ALLOWED_BUCKETS:
        return None

    weights = asset_buckets.get_bucket_weights(bucket)
    features = asset_buckets.get_bucket_features(bucket)

    # OHLCV 1h
    df = okx.get_ohlcv(inst_id, bar="1H", limit=240)
    if df.empty or len(df) < 60:
        logger.debug(f"{inst_id} : OHLCV insuffisant — skip")
        return None

    # ── 7 dimensions ──────────────────────────────────────────────────────────
    t = tech.compute_signal(df)
    score_tech = t.get("score", 0)

    ms = micro.analyze(inst_id)
    score_micro = ms.get("score", 0)

    fund = funding_score.get_funding_score(inst_id)
    score_funding = fund.get("score", 0)

    rg = regime.analyze(df) if features.get("use_volume_profile") or weights.get("regime", 0) > 0 \
         else {"regime": "unknown", "score": 0, "vol_regime": "normal", "position_multiplier": 1.0}
    score_regime = rg.get("score", 0)

    if features.get("use_volume_profile"):
        v = vp.analyze(df)
        score_vp = v.get("score", 0)
    else:
        score_vp = 0.0
        v = {}

    # News & Macro : gates binaires (placeholders Phase 3)
    news_gate = get_news_gate(inst_id, features)
    macro_gate = get_macro_gate(features)

    # ── Score composite pondéré ──────────────────────────────────────────────
    score = (
        score_tech    * weights.get("technique", 0)
        + score_micro * weights.get("microstructure", 0)
        + score_funding * weights.get("funding", 0)
        + score_regime  * weights.get("regime", 0)
        + score_vp      * weights.get("volume_profile", 0)
    )
    # News est intégré uniquement dans les buckets qui ont du poids news
    score += 0  # placeholder pour quand on intégrera le score news (vs gate)

    # Application des gates
    score = apply_gates(score, news_gate, macro_gate)

    # Clamp final
    score = round(max(-3.0, min(3.0, score)), 2)

    # ── Direction du signal ──────────────────────────────────────────────────
    side = "long" if score > 0 else "short" if score < 0 else None
    if side is None:
        return None
    # Backtest 90j a montré : shorts à 32% WR systématiquement perdants.
    if side not in cfg.ALLOWED_SIDES:
        return None

    # ── Filtre BTC régime (optionnel — désactivé par défaut) ────────────────
    if cfg.ENABLE_BTC_DIRECTIONAL_FILTER:
        if btc_regime is None:
            btc_regime = btc_regime_filter.get_regime()
        if side == "long" and not btc_regime.get("allow_long", True):
            logger.debug(f"{inst_id} : LONG bloqué par régime BTC")
            return None
        if side == "short" and not btc_regime.get("allow_short", True):
            logger.debug(f"{inst_id} : SHORT bloqué par régime BTC")
            return None

    # ── Seuil d'entrée par bucket + régime du ticker ─────────────────────────
    ticker_regime = rg.get("regime", "sideways")
    threshold = effective_threshold(bucket, ticker_regime)
    if abs(score) < threshold:
        return None

    return {
        "inst_id":      inst_id,
        "ticker":       inst_id.split("-")[0],
        "bucket":       bucket,
        "side":         side,
        "score":        score,
        "threshold":    threshold,
        "ticker_regime": ticker_regime,
        "score_tech":   score_tech,
        "score_micro":  score_micro,
        "score_funding": score_funding,
        "score_regime": score_regime,
        "score_vp":     score_vp,
        "news_gate":    news_gate,
        "macro_gate":   macro_gate,
        "verdict":      t.get("verdict", ""),
        "signaux":      t.get("signaux", []),
        "ms_signals":   ms.get("signals", []),
        "vp_data":      {k: v.get(k) for k in ("poc", "vah", "val", "in_value_area")} if v else {},
        "funding_pct":  fund.get("rate_pct", 0),
        "vol_annualized": rg.get("vol_annualized", 0),
        "position_multiplier": rg.get("position_multiplier", 1.0),
    }


# ─── Scan complet ────────────────────────────────────────────────────────────

def run_scan(limit_pairs: int | None = None) -> list[dict]:
    """
    Lance un scan complet des perps OKX.

    Args:
        limit_pairs : limite optionnelle (ex 20 pour smoke test rapide).
                      None = scanne toutes les paires éligibles.

    Returns:
        Liste de signaux actionnables triés par |score| décroissant.
    """
    logger.info("=== Scanner Bot 2 — début ===")

    # 1. Régime BTC global (mis en cache pour tous les tickers)
    btc_regime = btc_regime_filter.get_regime()
    logger.info(
        f"BTC regime: {btc_regime['state'].upper()} "
        f"(price ${btc_regime['btc_price']:,.0f} | dev {btc_regime['deviation_pct']:+.2f}%) "
        f"→ allow_long={btc_regime['allow_long']}, allow_short={btc_regime['allow_short']}"
    )

    # 2. Univers
    pairs = okx.get_swap_pairs(min_volume_usdt=cfg.PAIR_FILTER_MIN_VOLUME)
    if limit_pairs:
        pairs = pairs[:limit_pairs]
    logger.info(f"Univers à scanner : {len(pairs)} perps")

    # 3. Analyse parallèle des paires (gain ~5x vs séquentiel)
    signals = []
    completed = 0
    with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as ex:
        futures = {ex.submit(analyze_pair, inst_id, btc_regime): inst_id
                   for inst_id in pairs}
        for fut in as_completed(futures):
            inst_id = futures[fut]
            completed += 1
            try:
                result = fut.result()
                if result:
                    signals.append(result)
                    logger.info(
                        f"[{completed}/{len(pairs)}] {inst_id:25} "
                        f"score={result['score']:+.2f} | side={result['side'].upper()} | "
                        f"bucket={result['bucket']}"
                    )
            except Exception as e:
                logger.warning(f"[{completed}/{len(pairs)}] {inst_id} — erreur : {e}")

    # 4. Tri par |score| décroissant
    signals.sort(key=lambda s: abs(s["score"]), reverse=True)

    logger.info(f"=== Scan terminé : {len(signals)} signal(s) actionnable(s) ===")
    return signals


# ─── Smoke test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")

    print("=" * 60)
    print("  scanner_futures.py — smoke test sur 15 paires")
    print("=" * 60)

    # Limite à 15 paires pour aller vite (vrai scan complet = 99 paires, ~3-5 min)
    signals = run_scan(limit_pairs=15)

    print(f"\n[RÉSULTATS] {len(signals)} signal(s) actionnable(s) :\n")

    if not signals:
        print("  Aucun signal au-dessus des seuils.")
    else:
        # Tableau récap
        print(f"  {'#':3} {'Ticker':12} {'Bucket':8} {'Side':6} {'Score':7} {'Seuil':6} {'Régime':10}")
        print(f"  {'-'*3} {'-'*12} {'-'*8} {'-'*6} {'-'*7} {'-'*6} {'-'*10}")
        for i, s in enumerate(signals, 1):
            print(f"  {i:3} {s['ticker']:12} {s['bucket']:8} {s['side'].upper():6} "
                  f"{s['score']:+.2f}   {s['threshold']:.1f}   {s['ticker_regime']:10}")

        print(f"\n[DÉTAIL TOP 3]")
        for s in signals[:3]:
            print(f"\n  {s['ticker']} ({s['bucket']}, {s['side'].upper()}) — score {s['score']:+.2f}")
            print(f"    tech={s['score_tech']:+.2f} | micro={s['score_micro']:+.2f} | "
                  f"funding={s['score_funding']:+.2f} | regime={s['score_regime']:+.2f} | "
                  f"vp={s['score_vp']:+.2f}")
            print(f"    régime={s['ticker_regime']} | vol={s['vol_annualized']:.0f}%/an | "
                  f"mult={s['position_multiplier']}")
            for sig in s["signaux"][:2]:
                print(f"    - {sig}")

    print("\n" + "=" * 60)
