"""
Wrapper Telegram dédié Bot 2 — formats spécifiques long/short futures.

Bot 1 (alertes.py) est conçu pour les achats spot. Bot 2 a des formats
différents : direction (long/short), levier, marge engagée, prix de
liquidation, P&L sur la marge (pas le notional).

Utilise le bot Telegram dédié Bot 2 (TELEGRAM_TOKEN du .env portfolio-futures).
"""

import logging
import os

import requests
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# strip() défensif : tout caractère parasite (espace, tab, \n) collé avec
# le secret fait retourner Telegram un 404 silencieux ("Not Found").
TELEGRAM_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
TELEGRAM_CHAT_ID = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()


# ─── Envoi générique ──────────────────────────────────────────────────────────

def send(message: str, parse_mode: str = "Markdown") -> bool:
    """Envoie un message Telegram. Non-bloquant : retourne False si échec.

    Log le retour complet de Telegram pour pouvoir débugger les échecs
    silencieux (token corrompu, chat_id wrong, parse_mode error, etc.).
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error(
            "TELEGRAM env vars missing : token=%s chat_id=%s",
            "OK" if TELEGRAM_TOKEN else "MISSING",
            "OK" if TELEGRAM_CHAT_ID else "MISSING",
        )
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        data = resp.json()
        if not data.get("ok"):
            logger.error(
                "Telegram REFUSED status=%d error_code=%s desc=%s",
                resp.status_code,
                data.get("error_code"),
                data.get("description"),
            )
            return False
        return True
    except Exception as e:
        logger.error(f"Telegram exception : {e}")
        return False


# ─── Ouverture de position ────────────────────────────────────────────────────

def alerte_open_position(trade: dict):
    """
    trade attendu : dict retourné par paper_executor.open_position()
    """
    side = trade["side"].upper()
    direction_emoji = "📈" if side == "LONG" else "📉"
    leverage = trade["leverage"]

    msg = (
        f"🤖 *Bot 2 — Position OUVERTE*\n\n"
        f"{direction_emoji} *{side}* `{trade['ticker']}`\n"
        f"Entry : `${trade['entry_price']:,.4f}`\n"
        f"Quantité : `{trade['qty']:.6f}` ({trade['inst_id']})\n"
        f"\n"
        f"💰 Marge : `${trade['margin_usdt']:.2f}`\n"
        f"💼 Notional : `${trade['notional_usdt']:.2f}` (lev {leverage}x)\n"
        f"\n"
        f"🛡 SL : `${trade['sl_price']:,.4f}` (-3% prix)\n"
        f"🎯 TP : `${trade['tp_price']:,.4f}` (+4% prix)\n"
        f"⚡ Liq : `${trade['liq_price']:,.4f}`\n"
        f"⏱ Time stop : 24h\n"
        f"\n"
        f"📊 Score : `{trade['score']:+.2f}`\n"
        f"_PAPER TRADE — pas d'argent réel_"
    )
    send(msg)


# ─── Fermeture de position ────────────────────────────────────────────────────

def alerte_close_position(result: dict):
    """
    result attendu : dict retourné par paper_executor.close_position()
    """
    side = result["side"].upper()
    pnl_net = result.get("pnl_net_usdt", 0)
    pnl_pct = result.get("pnl_pct_margin", 0)
    reason = result.get("exit_reason", "unknown")

    win = pnl_net > 0
    emoji = "🟢" if win else "🔴"
    reason_label = {
        "tp": "🎯 TP atteint",
        "sl": "🛡 SL touché",
        "time_stop": "⏱ Time stop 24h",
        "liquidation": "⚡ LIQUIDATION",
    }.get(reason, reason)

    duration_h = (result["exit_ts"] - result["entry_ts"]) / 3600 if result.get("exit_ts") else 0

    msg = (
        f"🤖 *Bot 2 — Position FERMÉE*\n\n"
        f"{emoji} *{side}* `{result['ticker']}` — {reason_label}\n"
        f"\n"
        f"Entry  : `${result['entry_price']:,.4f}`\n"
        f"Exit   : `${result['exit_price']:,.4f}`\n"
        f"Durée  : `{duration_h:.1f}h`\n"
        f"\n"
        f"💵 P&L net : `${pnl_net:+.2f}`\n"
        f"📊 % marge : `{pnl_pct:+.2f}%`\n"
        f"\n"
        f"_PAPER TRADE — pas d'argent réel_"
    )
    send(msg)


# ─── Résumé de cycle ──────────────────────────────────────────────────────────

def alerte_cycle_summary(account_state: dict, open_positions: list[dict],
                         actions: list[dict], signals_taken: list[dict],
                         btc_regime: dict | None = None):
    """
    Résumé en fin de cycle. Affiche état compte + positions + actions.
    """
    equity = account_state["equity"]
    capital = account_state.get("capital_initial", equity)
    margin_used = account_state["margin_used"]
    nb_trades = account_state.get("nb_trades_total", 0)
    nb_wins = account_state.get("nb_wins", 0)
    nb_losses = account_state.get("nb_losses", 0)
    max_dd = account_state.get("max_drawdown_pct", 0)

    pnl_total = equity - capital
    pnl_pct = (pnl_total / capital * 100) if capital > 0 else 0
    emoji_pnl = "🟢" if pnl_total >= 0 else "🔴"
    winrate = (nb_wins / nb_trades * 100) if nb_trades > 0 else 0

    lines = [
        f"🤖 *Bot 2 — Cycle terminé*",
        f"",
        f"💼 Equity : `${equity:.2f}` (capital ${capital:.0f})",
        f"{emoji_pnl} P&L : `${pnl_total:+.2f}` ({pnl_pct:+.2f}%)",
        f"💰 Marge engagée : `${margin_used:.2f}` / libre `${equity - margin_used:.2f}`",
        f"📉 Max DD : `{max_dd:.2f}%`",
        f"",
    ]

    if btc_regime:
        state_emoji = {"bull": "📈", "bear": "📉", "neutral": "↔️"}.get(
            btc_regime["state"], "❓")
        lines.append(
            f"{state_emoji} BTC régime : *{btc_regime['state'].upper()}* "
            f"(${btc_regime['btc_price']:,.0f}, dev {btc_regime['deviation_pct']:+.1f}%)"
        )
        lines.append("")

    # Stats globales
    if nb_trades > 0:
        lines.append(
            f"📊 Trades : `{nb_trades}` | W: `{nb_wins}` | L: `{nb_losses}` | "
            f"Win rate : `{winrate:.0f}%`"
        )
        progress = min(nb_trades / 30 * 100, 100)
        lines.append(f"🎯 Progression validation : `{nb_trades}/30` ({progress:.0f}%)")
        lines.append("")

    # Positions ouvertes
    if open_positions:
        lines.append(f"*Positions ouvertes ({len(open_positions)}) :*")
        for p in open_positions[:5]:
            side_emoji = "📈" if p["side"] == "long" else "📉"
            age_h = (int(__import__('time').time()) - p["entry_ts"]) / 3600
            lines.append(
                f"{side_emoji} `{p['ticker']}` lev {p['leverage']:.0f}x — "
                f"`${p['entry_price']:,.4f}` ({age_h:.1f}h)"
            )
        if len(open_positions) > 5:
            lines.append(f"  _... et {len(open_positions) - 5} autres_")
        lines.append("")

    # Actions du cycle (trades complets retournés par position_manager)
    if actions:
        lines.append(f"*Sorties ce cycle ({len(actions)}) :*")
        for a in actions:
            emoji = "🟢" if (a.get("pnl_net_usdt", 0) or 0) > 0 else "🔴"
            reason = a.get("exit_reason") or a.get("reason", "?")
            lines.append(
                f"{emoji} `{a['ticker']}` {a['side'].upper()} — {reason} "
                f"`${a.get('pnl_net_usdt', 0):+.2f}`"
            )
        lines.append("")

    if signals_taken:
        lines.append(f"*Entrées ce cycle ({len(signals_taken)}) :*")
        for s in signals_taken:
            side_emoji = "📈" if s["side"] == "long" else "📉"
            lines.append(
                f"{side_emoji} `{s['ticker']}` ({s['bucket']}) — score `{s['score']:+.2f}`"
            )
        lines.append("")

    if not actions and not signals_taken and not open_positions:
        lines.append("_Aucune action ce cycle — attente de setup_")

    send("\n".join(lines))


# ─── Crash alert ──────────────────────────────────────────────────────────────

def alerte_crash(checkpoint: str, error: str):
    """Crash inattendu pendant le cycle."""
    send(
        f"💥 *Bot 2 — CRASH cycle*\n\n"
        f"Checkpoint : `{checkpoint}`\n"
        f"Erreur :\n```\n{error[:1500]}\n```"
    )


# ─── Smoke test ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    ok = send("🧪 *Bot 2 — alertes_futures test*\n_Smoke test du module alertes._")
    print(f"Test Telegram : {'OK' if ok else 'ECHEC'}")
