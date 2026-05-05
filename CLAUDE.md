# Bot 2 — Futures Swing Trading (OKX Perpetual Swaps)

> Ce fichier est lu en priorité par Claude Code à chaque session.
> Contient les **règles fondamentales**, les **conventions de code**, et les **décisions
> architecturales validées**. À mettre à jour si une décision change.

---

## 🎯 Mission

Bot 2 = clone Bot 1 adapté au court terme sur perps OKX, avec levier.

- **Cycle** : 1h (vs 4h Bot 1)
- **Levier** : 2x défaut, 3x sur setups A+ (score >= 2.5)
- **TP** : +4% prix sous-jacent (vs +12% Bot 1)
- **SL** : -3% prix sous-jacent (vs ATR -4%/-10% Bot 1)
- **Time stop** : 24h max par position (vs 7j Bot 1)
- **Long ET short** (vs Bot 1 long-only)
- **Mode** : paper trading **uniquement** pour l'instant — compte démo OKX EEA

**Validation avant live** : 30 paper trades minimum.

---

## 🚨 Règles fondamentales (héritées Bot 1, NON-négociables)

1. **Aucun LLM dans le chemin critique** — décision finale d'entrée/sortie/sizing = 100% déterministe. Les agents IA enrichissent (news, régime, reflect post-trade) mais ne tranchent jamais.
2. **Filtre BTC MA50 inversé** : BTC > MA50 → longs autorisés. BTC < MA50 → shorts autorisés. Hystérésis ±1.5% + cooldown 4h pour éviter le whipsaw. Calculé sur **bougies 4h**, pas 1h.
3. **Architecture non-bloquante** — chaque module isolé en `try/except`. Un module qui crash ne tue pas le cycle.
4. **Score composite minimum 1.7** pour déclencher entrée (vs 1.5 Bot 1, durci à cause du levier).
5. **Risque maximum par trade** : 1.98% du capital (notional max 66% × stop 3%, sur tier A+).

---

## 🏗️ Architecture

```
portfolio-futures/
├── .env                          ← Telegram + clés démo OKX (NE JAMAIS commit)
├── .gitignore                    ← protège .env + data/*.db + __pycache__
├── CLAUDE.md                     ← ce fichier
│
├── config_futures.py             ← TOUTES les constantes stratégiques
├── okx_futures.py                ← client OKX SWAP read-only (EEA démo)
│
│   PHASE 1 — Paper engine ✅
├── paper_account.py              ← état simulé (equity, marge, drawdown, winrate)
├── paper_executor.py             ← open/close + fees + slippage + funding + liquidation
├── position_manager_futures.py   ← surveillance SL/TP/time stop + wick detection
│
│   PHASE 2 — Signal engine (en cours)
├── asset_buckets.py              ← classification majors/mid-caps/memes
├── btc_regime_filter.py          ← hystérésis MA50 4h + cooldown 4h
├── funding_score.py              ← 7e dimension scoring funding rate
├── technical_signals_1h.py       ← adaptation Bot 1 pour TF 1h
├── microstructure_1h.py          ← adaptation Bot 1 pour SWAP + TF 1h
├── regime_detector_1h.py         ← adaptation Bot 1 (HMM/GARCH sur 1h)
├── volume_profile_1h.py          ← adaptation Bot 1
└── scanner_futures.py            ← orchestrateur (compose le score, déclenche)
│
│   PHASE 3 — Boucle (à venir)
├── run_once_futures.py           ← point d'entrée cycle 1h
├── alertes_futures.py            ← Telegram (formats long/short futures)
└── data/
    └── paper_trades.db           ← SQLite (trades + état compte + equity curve)
```

---

## 📊 Pondération du score (par classe d'actif)

L'agent Plan a corrigé la pondération naïve "Bot 1 + funding" pour adapter à l'intraday 1h
et à la diversité des paires perps OKX.

### Majors (BTC, ETH, SOL, BNB, XRP, ADA, DOGE)

```
Technique         45%   ← lookback réduit pour TF 1h
Microstructure    20%   ← order flow, OI delta, taker ratio
News              15%   ← gates binaires si événement majeur
Funding rate      10%   ← contrarian si extrême
Régime            5%    ← module le seuil d'entrée, pas le score
Volume Profile    5%
Macro             0%    ← filtre on/off uniquement
```

### Mid-caps (LINK, AVAX, INJ, NEAR, ARB, OP, etc.)

```
Technique         55%
Microstructure    25%
News              5%
Funding rate      10%
Régime            5%
Volume Profile    0%
```

### Memes (PEPE, FLOKI, CAT, HMSTR, BOME, SHIB, etc.)

```
Technique         65%
Microstructure    30%
Funding rate      5%
News              0%   ← désactivé (sauf listings/délistages)
Macro             0%   ← désactivé (pas de pouvoir prédictif)
Régime BTC        0%   ← désactivé (decoupling fréquent)
```

---

## 🎚️ Régime → seuil d'entrée

Plutôt que d'avoir le régime comme composante du score, il **module le seuil** :

| Régime détecté | Seuil minimum |
|---|---|
| Trending (bull ou bear net) | 1.5 |
| Range (oscillation) | 2.0 |
| Choppy (pas de structure) | 2.5 ou skip total |

---

## 💾 Persistance & mémoire

- **SQLite** `data/paper_trades.db` :
  - `paper_trades` — historique complet des trades
  - `paper_account` — état singleton (equity, marge, drawdown)
  - `paper_equity_curve` — snapshot equity à chaque cycle
- **Mémoire vectorielle ruflo** (Phase 3) :
  - Namespace `bot2` (isolé du Bot 1 qui est sur `default`)
  - Stockage des leçons post-trade via Reflect Agent

---

## 🔐 Secrets & sécurité

- **Jamais** committer `.env` — déjà dans `.gitignore`
- En GitHub Actions : utiliser **Secrets** (DEMO_OKX_API_KEY, TELEGRAM_TOKEN, etc.)
- Le mode démo OKX EEA passe par `eea.okx.com` (pas `www.okx.com` global) avec
  header `x-simulated-trading: 1` sur toutes les requêtes auth + public

---

## 🧪 Tests

Chaque module a un smoke test dans `if __name__ == "__main__"`. À lancer après modification :

```bash
python paper_account.py            # singleton SQLite, état initial
python paper_executor.py           # ouvre + ferme un trade BTC, P&L cohérent
python position_manager_futures.py # surveillance positions ouvertes
python okx_futures.py              # 5 endpoints OKX
python scanner_futures.py          # scan complet 99 paires (à venir)
```

---

## 🚀 Déploiement

- **Repo** : `NIC-CLH/trading-bot-futures` (public, GitHub Actions illimitées)
- **Cycle** : cron 1h (`0 * * * *`) en GitHub Actions, à activer après validation paper
- **Phase paper** : exécution locale OU GitHub Actions, validée sur 30 trades minimum
- **Phase live** : décision séparée, après audit complet du paper

---

## 📌 Décisions actées (pour ne pas y revenir)

- ✅ Bot 2 séparé totalement de Bot 1 (pas d'import croisé, code dupliqué quand nécessaire)
- ✅ Levier 2x défaut, 3x uniquement sur score >= 2.5
- ✅ Score min 1.7 (vs Bot 1 = 1.5)
- ✅ Tiers de sizing identiques Bot 1 : 12% / 17% / 22% du capital en marge
- ✅ Mode démo OKX EEA, pas simulation 100% locale (clés déjà créées)
- ✅ `paper/` engine local, pas de fork de Bot 1 execution
- ✅ Ruflo namespace `bot2`
- ✅ GitHub Actions cron 1h (après validation paper)

---

## ⚠️ Pièges connus

1. **Endpoint OKX démo** : `eea.okx.com`, PAS `www.okx.com`. Sinon erreur 401 "API key doesn't exist".
2. **`technical_signals.py` Bot 1 est conçu pour daily** (lookback 200 EMA, 30j min). Adapter avec lookback réduit pour TF 1h.
3. **`market_microstructure.py` a `period="5m"` hardcodé** — paramétriser avant utilisation 1h.
4. **`hmmlearn`, `arch`, `ruptures`** sont importés par `regime_detector.py` mais non listés dans
   le `requirements.txt` Bot 1. Vérifier installation avant prod.
5. **Top volume perps OKX = memecoins** (CAT, FLOKI, PEPE, SHIB...) — d'où la nécessité du
   bucketing pour ne pas appliquer une logique majors-only à des memes.

---

## 📞 Telegram

- Bot dédié Bot 2 (séparé du Bot 1)
- Token et chat_id dans `.env`
- Format des alertes : prefixe 🤖 pour le distinguer du Bot 1 spot
