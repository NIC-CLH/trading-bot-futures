"""
Classification des paires perps OKX en 3 buckets stratégiques.

Le scanner_futures applique des pondérations différentes selon le bucket :

  Majors  : Tech 45 / News 15 / Micro 20 / Funding 10 / Régime 5 / VP 5
  Midcap  : Tech 55 / News  5 / Micro 25 / Funding 10 / Régime 5 / VP 0
  Memes   : Tech 65 / News  0 / Micro 30 / Funding  5 (Macro/Régime BTC désactivés)

Justification (issue de l'audit Plan agent) :
  - Les memecoins decouplent fréquemment du BTC → régime BTC peu prédictif
  - Les memes ont peu de news structurées (sauf listings/délistages) → désactivé
  - L'edge sur memes vient du flux et du funding extrême (taux contrarian)
  - Sur les majors, news + macro + volume profile valent encore quelque chose
"""

# ─── Listes explicites ────────────────────────────────────────────────────────

# Top crypto par market cap, avec liquidity profonde et news structurées
MAJORS = {
    "BTC", "ETH", "SOL", "BNB", "XRP", "ADA", "DOGE",
}

# Mid-caps = L1/L2/DeFi établis (~$1B+ mcap typique)
MIDCAPS = {
    # Layer 1 / Layer 2
    "LINK", "AVAX", "DOT", "MATIC", "LTC", "BCH", "ATOM", "FIL", "ETC",
    "XLM", "VET", "ICP", "ALGO", "EGLD", "FTM", "TRX", "INJ", "SUI",
    "APT", "NEAR", "ARB", "OP", "TIA", "SEI", "STRK", "DYM", "MANTA",
    # DeFi blue chips
    "UNI", "AAVE", "MKR", "CRV", "LDO", "SNX", "COMP", "1INCH", "GMX",
    # Récents établis
    "BLUR", "JTO", "JUP", "PYTH", "WLD", "ENA", "ETHFI", "ONDO",
    "ZK", "ZRO", "EIGEN", "REZ", "IO", "AERO", "MORPHO",
}

# Patterns indiquant un memecoin (matched contre le ticker base)
MEME_PATTERNS = (
    "PEPE", "FLOKI", "SHIB", "BONK", "WIF", "BOME", "MEME", "POPCAT",
    "MEW", "GOAT", "BRETT", "CHILLGUY", "FARTCOIN", "PNUT",
    "HMSTR", "DOGS", "GMT", "TURBO", "BABY", "TRUMP", "1000",
    "MOG", "NEIRO", "DEGEN", "MOODENG", "BAN", "USELESS",
    "PIPPIN", "AI16Z", "ZEREBRO", "GIGA", "MICHI", "RETARDIO",
    "CAT",  # CATs (HMSTR-like, attention conflit avec MidnightCAT etc.)
    "NOT",  # NOT coin (Notcoin)
)

# Stables + wrapped — exclus du scan (rien à scorer dessus)
EXCLUDE = {
    "USDT", "USDC", "BUSD", "DAI", "FDUSD", "TUSD", "USDP", "EURC",
    "WBTC", "WETH", "STETH", "BETH", "BBTC",
}


# ─── API publique ─────────────────────────────────────────────────────────────

def extract_base(inst_id: str) -> str:
    """'BTC-USDT-SWAP' → 'BTC'."""
    return inst_id.split("-")[0].upper()


def classify(inst_id: str) -> str:
    """
    Retourne 'majors' | 'midcap' | 'memes' | 'exclude'.

    Ordre de priorité :
      1. EXCLUDE   → 'exclude'  (stables, wrapped — rien à trader)
      2. MAJORS    → 'majors'   (DOGE résolu ici malgré son pattern meme)
      3. MEME hit  → 'memes'    (substring match)
      4. MIDCAPS   → 'midcap'
      5. fallback  → 'memes'    (conservatif : token inconnu = traité comme meme,
                                 ce qui désactive les modules news/macro/régime BTC
                                 qui n'ont aucun pouvoir prédictif sur des inconnus)
    """
    base = extract_base(inst_id)

    if base in EXCLUDE:
        return "exclude"
    if base in MAJORS:
        return "majors"

    for pat in MEME_PATTERNS:
        if pat in base:
            return "memes"

    if base in MIDCAPS:
        return "midcap"

    return "memes"  # fallback conservatif


def get_bucket_weights(bucket: str) -> dict[str, float]:
    """
    Pondérations des dimensions de score (somme = 1.0).
    News & Macro sont gérés séparément en gates binaires (cf. news_macro_gates.py).
    Régime BTC ajuste le SEUIL d'entrée (cf. btc_regime_filter.py).
    """
    if bucket == "majors":
        return {
            "technique":      0.45,
            "microstructure": 0.20,
            "news":           0.15,
            "funding":        0.10,
            "regime":         0.05,
            "volume_profile": 0.05,
        }
    if bucket == "midcap":
        return {
            "technique":      0.55,
            "microstructure": 0.25,
            "news":           0.05,
            "funding":        0.10,
            "regime":         0.05,
            "volume_profile": 0.00,
        }
    if bucket == "memes":
        return {
            "technique":      0.65,
            "microstructure": 0.30,
            "news":           0.00,
            "funding":        0.05,
            "regime":         0.00,
            "volume_profile": 0.00,
        }
    raise ValueError(f"Bucket inconnu : {bucket}")


def get_bucket_features(bucket: str) -> dict[str, bool]:
    """
    Flags features actives par bucket. Le scanner skip les modules désactivés
    pour gagner du temps API et éviter d'inclure du bruit dans le score.
    """
    if bucket == "majors":
        return {
            "use_macro_gate":     True,
            "use_btc_regime":     True,
            "use_news_gate":      True,
            "use_volume_profile": True,
        }
    if bucket == "midcap":
        return {
            "use_macro_gate":     True,
            "use_btc_regime":     True,
            "use_news_gate":      True,
            "use_volume_profile": False,
        }
    if bucket == "memes":
        return {
            "use_macro_gate":     False,
            "use_btc_regime":     False,
            "use_news_gate":      False,
            "use_volume_profile": False,
        }
    raise ValueError(f"Bucket inconnu : {bucket}")


def is_tradable(inst_id: str) -> bool:
    """Le scanner doit-il scorer ce ticker ?"""
    return classify(inst_id) != "exclude"


# ─── Smoke test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import okx_futures as okx

    print("=" * 60)
    print("  asset_buckets.py — classification des perps OKX")
    print("=" * 60)

    pairs = okx.get_swap_pairs(min_volume_usdt=500_000)

    counts = {"majors": 0, "midcap": 0, "memes": 0, "exclude": 0}
    samples: dict[str, list[str]] = {"majors": [], "midcap": [], "memes": []}

    for inst_id in pairs:
        bucket = classify(inst_id)
        counts[bucket] = counts.get(bucket, 0) + 1
        if bucket in samples and len(samples[bucket]) < 12:
            samples[bucket].append(extract_base(inst_id))

    print(f"\n[1] Univers : {len(pairs)} perps")
    print(f"\n[2] Repartition :")
    total = sum(counts.values())
    for b, n in counts.items():
        pct = n / total * 100 if total else 0
        print(f"    {b:8} : {n:3} ({pct:.1f}%)")

    print(f"\n[3] Echantillons :")
    for b, lst in samples.items():
        print(f"    {b:8} : {', '.join(lst)}")

    print(f"\n[4] Poids des dimensions par bucket :")
    for b in ("majors", "midcap", "memes"):
        w = get_bucket_weights(b)
        line = " | ".join(f"{k}={v:.2f}" for k, v in w.items())
        print(f"    {b:8}: {line}")

    print("\n" + "=" * 60)
