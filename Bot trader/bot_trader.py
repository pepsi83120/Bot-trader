"""
Bot Telegram MT5 — Version corrigée complète
Tous les bugs critiques corrigés + EA MQ5 intégré
"""

import os
import json
import time
import asyncio
import logging
import functools
import pandas as pd
from datetime import datetime
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

# ============================================================
#  ⚙️  CHARGEMENT CONFIG depuis .env
# ============================================================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "METS_TON_TOKEN_ICI")
ADMIN_CHAT_ID  = int(os.getenv("ADMIN_CHAT_ID", "0"))

SYMBOLS        = [
    # ── Forex majeurs (les plus tradés au monde) ──
    "EURUSD",   # Euro / Dollar
    "GBPUSD",   # Livre / Dollar
    "USDJPY",   # Dollar / Yen
    "USDCHF",   # Dollar / Franc suisse
    "AUDUSD",   # Australien / Dollar
    "USDCAD",   # Dollar / Canadien
    "NZDUSD",   # Néo-zélandais / Dollar
    # ── Forex croisées populaires ──
    "EURGBP",   # Euro / Livre
    "EURJPY",   # Euro / Yen
    "GBPJPY",   # Livre / Yen
    # ── Or & Matières premières ──
    "XAUUSD",   # Or (le plus tradé)
    "XAGUSD",   # Argent
    "USOIL",    # Pétrole WTI
    "UKOIL",    # Pétrole Brent
    # ── Crypto (top par volume) ──
    "BTCUSD",   # Bitcoin
    "ETHUSD",   # Ethereum
    "XRPUSD",   # Ripple
    "LTCUSD",   # Litecoin
    "BNBUSD",   # BNB
    "SOLUSD",   # Solana
    # ── Indices boursiers ──
    "US500",    # S&P 500
    "US100",    # Nasdaq
    "GER40",    # DAX Allemagne
    "UK100",    # FTSE 100
]
DEFAULT_LOT    = 0.01
SCAN_INTERVAL  = 300       # secondes entre chaque scan auto
SIGNAL_TTL     = 600       # secondes avant expiration d'un signal (10 min)
CMD_TIMEOUT    = 8.0       # timeout EA en secondes

# Dossier partagé MT5 — configurable via .env
MT5_COMMON = os.getenv(
    "MT5_COMMON_PATH",
    os.path.expandvars(r"%APPDATA%\MetaQuotes\Terminal\Common\Files")
)
ORDER_FILE  = os.path.join(MT5_COMMON, "telegram_order.txt")
RESULT_FILE = os.path.join(MT5_COMMON, "telegram_result.txt")

# Fichiers de persistance
USERS_FILE = "authorized_users.json"
STATS_FILE = "session_stats.json"

# ============================================================
#  LOGGING
# ============================================================
logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ============================================================
#  ÉTAT GLOBAL
# ============================================================
authorized_users: set[int] = {ADMIN_CHAT_ID}
pending_signals: dict[str, dict] = {}
session_stats = {"trades": 0, "wins": 0, "losses": 0, "profit": 0.0}

# Lock pour éviter la race condition sur les fichiers MT5
_mt5_lock = asyncio.Lock()

# ============================================================
#  PERSISTANCE
# ============================================================
def load_users():
    global authorized_users
    try:
        with open(USERS_FILE, "r") as f:
            data = json.load(f)
            authorized_users = set(data) | {ADMIN_CHAT_ID}
            logger.info(f"✅ {len(authorized_users)} utilisateurs chargés")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"Erreur chargement users: {e}")

def save_users():
    try:
        with open(USERS_FILE, "w") as f:
            json.dump(list(authorized_users), f)
    except Exception as e:
        logger.error(f"Erreur sauvegarde users: {e}")

def load_stats():
    global session_stats
    try:
        with open(STATS_FILE, "r") as f:
            session_stats = json.load(f)
            logger.info("✅ Stats chargées")
    except FileNotFoundError:
        pass
    except Exception as e:
        logger.error(f"Erreur chargement stats: {e}")

def save_stats():
    try:
        with open(STATS_FILE, "w") as f:
            json.dump(session_stats, f)
    except Exception as e:
        logger.error(f"Erreur sauvegarde stats: {e}")

# ============================================================
#  BRIDGE MT5 — VERSION ASYNC CORRIGÉE
# ============================================================
def _write_command(cmd: str):
    """Écrit la commande dans le fichier ORDER (synchrone, exécuté en executor)."""
    if os.path.exists(RESULT_FILE):
        os.remove(RESULT_FILE)
    # Écriture en UTF-8 sans BOM pour compatibilité MQ5
    with open(ORDER_FILE, "w", encoding="utf-8", newline="\n") as f:
        f.write(cmd.strip())

def _read_result() -> str | None:
    """Attend et lit le fichier RESULT (synchrone, exécuté en executor)."""
    deadline = time.time() + CMD_TIMEOUT
    while time.time() < deadline:
        if os.path.exists(RESULT_FILE):
            with open(RESULT_FILE, "r", encoding="latin-1") as f:
                result = f.read().strip()
            try:
                os.remove(RESULT_FILE)
            except Exception:
                pass
            return result
        time.sleep(0.2)
    return None

async def send_command(cmd: str) -> str | None:
    """
    Envoie une commande à l'EA et attend le résultat.
    - Async : ne bloque pas l'event loop
    - Protégé par un Lock : pas de race condition
    """
    async with _mt5_lock:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _write_command, cmd)
        result = await loop.run_in_executor(None, _read_result)
        return result

def parse_result(result: str | None) -> tuple[bool, str]:
    """Transforme la réponse brute de l'EA en (succès, message lisible)."""
    if result is None:
        return False, (
            "⏱️ *Timeout* — L'EA n'a pas répondu.\n"
            "_Vérifie que TelegramBridge tourne sur un graphique dans MT5._"
        )
    parts = result.split("|")
    if not parts:
        return False, "❌ Réponse EA vide."
    if parts[0] == "OK":
        return True, "\n".join(parts[1:])
    else:
        return False, "❌ Erreur EA : " + " | ".join(parts[1:])

async def place_order(symbol: str, direction: str, lot: float,
                      sl: float, tp: float) -> tuple[bool, str]:
    # Format strict : DIRECTION|SYMBOL|LOT|SL|TP (sans espaces)
    cmd = f"{direction}|{symbol}|{lot:.2f}|{sl:.5f}|{tp:.5f}"
    result = await send_command(cmd)
    ok, msg = parse_result(result)
    if ok:
        parts = [p for p in msg.split("\n") if p.strip()]
        info = "\n".join(f"▸ {p}" for p in parts) if parts else msg
        # Mise à jour stats
        session_stats["trades"] += 1
        save_stats()
        return True, f"✅ *Ordre exécuté !*\n{info}"
    return False, msg

async def get_balance_text() -> str:
    result = await send_command("INFO")
    ok, msg = parse_result(result)
    if not ok:
        return msg
    data = {}
    for item in msg.split("|"):
        if ":" in item:
            k, v = item.split(":", 1)
            data[k.strip()] = v.strip()
    return (
        f"💰 *Compte MT5*\n"
        f"▸ Solde      : `{data.get('BALANCE','?')} {data.get('CURRENCY','')}`\n"
        f"▸ Équité     : `{data.get('EQUITY','?')} {data.get('CURRENCY','')}`\n"
        f"▸ Marge      : `{data.get('MARGIN','?')} {data.get('CURRENCY','')}`\n"
        f"▸ Marge libre: `{data.get('FREE','?')} {data.get('CURRENCY','')}`"
    )

async def get_positions_text() -> str:
    result = await send_command("POSITIONS")
    ok, msg = parse_result(result)
    if not ok:
        return msg
    if not msg or "NONE" in msg:
        return "📭 *Aucune position ouverte.*"
    lines = ["📋 *Positions ouvertes :*\n"]
    for part in msg.split("|"):
        if part.startswith("POS:"):
            fields = part.split(":")[1:]
            if len(fields) < 9:
                logger.warning(f"Position mal formée: {part}")
                continue
            try:
                ticket, sym, direction, vol, popen, pcur, psl, ptp, profit = fields[:9]
                emoji = "🟢" if float(profit) >= 0 else "🔴"
                lines.append(
                    f"{emoji} *{sym}* — {direction} | Lot: `{vol}`\n"
                    f"   Ouvert: `{popen}` | Actuel: `{pcur}`\n"
                    f"   SL: `{psl}` | TP: `{ptp}`\n"
                    f"   P&L: `{profit}$` | Ticket: `{ticket}`\n"
                )
            except (ValueError, IndexError) as e:
                logger.warning(f"Erreur parsing position: {e}")
                continue
    return "\n".join(lines)

# ============================================================
#  ANALYSE TECHNIQUE
# ============================================================
_signal_cache: dict[str, dict] = {}  # {symbol: {"ts": float, "data": df}}

def get_signal(symbol: str) -> dict | None:
    try:
        import yfinance as yf
        yf_map = {
            # Forex majeurs
            "EURUSD": "EURUSD=X",
            "GBPUSD": "GBPUSD=X",
            "USDJPY": "USDJPY=X",
            "USDCHF": "USDCHF=X",
            "AUDUSD": "AUDUSD=X",
            "USDCAD": "USDCAD=X",
            "NZDUSD": "NZDUSD=X",
            # Forex croisées
            "EURGBP": "EURGBP=X",
            "EURJPY": "EURJPY=X",
            "GBPJPY": "GBPJPY=X",
            # Or & Matières premières
            "XAUUSD": "GC=F",
            "XAGUSD": "SI=F",
            "USOIL":  "CL=F",
            "UKOIL":  "BZ=F",
            # Crypto
            "BTCUSD": "BTC-USD",
            "ETHUSD": "ETH-USD",
            "XRPUSD": "XRP-USD",
            "LTCUSD": "LTC-USD",
            "BNBUSD": "BNB-USD",
            "SOLUSD": "SOL-USD",
            # Indices
            "US500":  "^GSPC",
            "US100":  "^NDX",
            "GER40":  "^GDAXI",
            "UK100":  "^FTSE",
        }
        ticker = yf_map.get(symbol)
        if not ticker:
            logger.warning(f"Symbole non mappé: {symbol}")
            return None

        # Cache 5 minutes
        cache = _signal_cache.get(symbol)
        if cache and (time.time() - cache["ts"]) < 300:
            df = cache["df"]
        else:
            df = yf.download(ticker, period="5d", interval="15m", progress=False)
            if df is None or len(df) < 50:
                return None
            df = df[["Close", "High", "Low"]].copy()
            df.columns = ["close", "high", "low"]
            _signal_cache[symbol] = {"ts": time.time(), "df": df}

        # EMA
        df["ema50"]  = df["close"].ewm(span=50).mean()
        df["ema200"] = df["close"].ewm(span=200).mean()

        # RSI — protégé contre division par zéro
        delta = df["close"].diff()
        gain  = delta.where(delta > 0, 0).rolling(14).mean()
        loss  = (-delta.where(delta < 0, 0)).rolling(14).mean()
        loss  = loss.replace(0, 1e-10)  # évite ZeroDivisionError
        df["rsi"] = 100 - (100 / (1 + gain / loss))

        # MACD
        ema12 = df["close"].ewm(span=12).mean()
        ema26 = df["close"].ewm(span=26).mean()
        df["macd"]      = ema12 - ema26
        df["macd_sig"]  = df["macd"].ewm(span=9).mean()
        df["macd_hist"] = df["macd"] - df["macd_sig"]

        # ATR
        df["tr"] = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"]  - df["close"].shift()).abs(),
        ], axis=1).max(axis=1)
        atr = df["tr"].rolling(20).mean().iloc[-1]

        last  = df.iloc[-1]
        prev  = df.iloc[-2]
        price = float(last["close"])
        direction = None

        if (last["ema50"] > last["ema200"]
                and last["rsi"] < 40
                and last["macd_hist"] > 0
                and prev["macd_hist"] < last["macd_hist"]):
            direction = "BUY"
        elif (last["ema50"] < last["ema200"]
                and last["rsi"] > 60
                and last["macd_hist"] < 0
                and prev["macd_hist"] > last["macd_hist"]):
            direction = "SELL"

        if direction is None:
            return None

        sl_dist = float(atr) * 1.5
        tp_dist = float(atr) * 3.0
        sl = round(price - sl_dist if direction == "BUY" else price + sl_dist, 5)
        tp = round(price + tp_dist if direction == "BUY" else price - tp_dist, 5)

        return {
            "symbol":    symbol,
            "direction": direction,
            "price":     round(price, 5),
            "sl":        sl,
            "tp":        tp,
            "lot":       DEFAULT_LOT,
            "rsi":       round(float(last["rsi"]), 1),
            "ts":        time.time(),  # timestamp pour TTL
        }
    except Exception as e:
        logger.error(f"Erreur analyse {symbol}: {e}")
        return None

def format_signal_message(sig: dict, signal_id: str) -> tuple[str, InlineKeyboardMarkup]:
    emoji = "📈" if sig["direction"] == "BUY" else "📉"
    sl_dist = abs(sig["sl"] - sig["price"])
    tp_dist = abs(sig["tp"] - sig["price"])
    rr = round(tp_dist / max(sl_dist, 1e-10), 2)
    text = (
        f"🔔 *SIGNAL DÉTECTÉ*\n\n"
        f"{emoji} *{sig['direction']} {sig['symbol']}*\n\n"
        f"▸ Prix actuel : `{sig['price']}`\n"
        f"▸ Lot         : `{sig['lot']}`\n"
        f"▸ Stop Loss   : `{sig['sl']}`\n"
        f"▸ Take Profit : `{sig['tp']}`\n"
        f"▸ R/R         : `1:{rr}`\n"
        f"▸ RSI         : `{sig['rsi']}`\n\n"
        f"Lance le trade ou refuse :"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Valider",  callback_data=f"validate_{signal_id}"),
        InlineKeyboardButton("❌ Refuser", callback_data=f"reject_{signal_id}"),
    ]])
    return text, keyboard

# ============================================================
#  SÉCURITÉ — DÉCORATEURS CORRIGÉS (functools.wraps)
# ============================================================
def is_authorized(uid: int) -> bool:
    return uid in authorized_users

def is_admin(uid: int) -> bool:
    return uid == ADMIN_CHAT_ID

def auth_check(func):
    @functools.wraps(func)  # ← CORRIGÉ : évite le conflit de noms dans PTB
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update.effective_user.id):
            await update.message.reply_text("⛔ Accès refusé.")
            return
        return await func(update, context)
    return wrapper

def admin_check(func):
    @functools.wraps(func)  # ← CORRIGÉ
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Commande réservée à l'admin.")
            return
        return await func(update, context)
    return wrapper

# ============================================================
#  COMMANDES
# ============================================================
@auth_check
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Bot MT5 — Menu des commandes*\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📊 *INFORMATIONS*\n"
        "▸ /solde — Solde, équité, marge\n"
        "▸ /positions — Positions ouvertes\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🛒 *ORDRES MANUELS*\n"
        "▸ /buy SYMBOL LOT SL TP\n"
        "   _Ex: /buy XAUUSD 0.01 1900 1960_\n"
        "▸ /sell SYMBOL LOT SL TP\n"
        "   _Ex: /sell BTCUSD 0.01 65000 60000_\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔒 *FERMETURE*\n"
        "▸ /close TICKET — Ferme une position\n"
        "▸ /closeall — Ferme tout\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔔 *SIGNAUX*\n"
        "▸ /signal — Analyse et propose un trade\n""▸ /conseil SYMBOL — Conseils LOT/SL/TP sur un actif\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📈 *STATS*\n"
        "▸ /stats — Statistiques de session\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👑 *ADMIN UNIQUEMENT*\n"
        "▸ /adduser ID — Ajouter un utilisateur\n"
        "▸ /removeuser ID — Retirer un utilisateur\n"
        "▸ /users — Liste des utilisateurs\n",
        parse_mode="Markdown"
    )

@auth_check
async def cmd_solde(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await get_balance_text()
    await update.message.reply_text(msg, parse_mode="Markdown")

@auth_check
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await get_positions_text()
    await update.message.reply_text(msg, parse_mode="Markdown")

@auth_check
async def cmd_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "❌ Usage : `/buy SYMBOL LOT SL TP`\n_Ex: /buy XAUUSD 0.01 1900 1960_",
            parse_mode="Markdown"
        )
        return
    try:
        symbol = args[0].upper()
        lot    = float(args[1])
        sl     = float(args[2])
        tp     = float(args[3])
    except ValueError:
        await update.message.reply_text("❌ Paramètres invalides. LOT, SL et TP doivent être des nombres.")
        return
    await update.message.reply_text(f"⏳ Envoi ordre BUY {symbol}...")
    ok, msg = await place_order(symbol, "BUY", lot, sl, tp)
    await update.message.reply_text(msg, parse_mode="Markdown")

@auth_check
async def cmd_sell(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 4:
        await update.message.reply_text(
            "❌ Usage : `/sell SYMBOL LOT SL TP`\n_Ex: /sell BTCUSD 0.01 65000 60000_",
            parse_mode="Markdown"
        )
        return
    try:
        symbol = args[0].upper()
        lot    = float(args[1])
        sl     = float(args[2])
        tp     = float(args[3])
    except ValueError:
        await update.message.reply_text("❌ Paramètres invalides. LOT, SL et TP doivent être des nombres.")
        return
    await update.message.reply_text(f"⏳ Envoi ordre SELL {symbol}...")
    ok, msg = await place_order(symbol, "SELL", lot, sl, tp)
    await update.message.reply_text(msg, parse_mode="Markdown")

@auth_check
async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage : `/close TICKET`", parse_mode="Markdown")
        return
    ticket = context.args[0]
    await update.message.reply_text(f"⏳ Fermeture ticket {ticket}...")
    result = await send_command(f"CLOSE|{ticket}")
    ok, msg = parse_result(result)
    if ok:
        session_stats["trades"] = max(0, session_stats["trades"])
        save_stats()
    await update.message.reply_text(msg, parse_mode="Markdown")

@auth_check
async def cmd_closeall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Fermeture de toutes les positions...")
    result = await send_command("CLOSEALL")
    ok, msg = parse_result(result)
    await update.message.reply_text(msg, parse_mode="Markdown")

@auth_check
async def cmd_signal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Analyse en cours...")
    found = False
    for symbol in SYMBOLS:
        sig = get_signal(symbol)
        if sig:
            found = True
            signal_id = f"{symbol}_{int(time.time())}"
            pending_signals[signal_id] = sig
            text, keyboard = format_signal_message(sig, signal_id)
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
    if not found:
        await update.message.reply_text(
            "😴 *Aucun signal clair détecté.*\n_Réessaie dans quelques minutes._",
            parse_mode="Markdown"
        )


@auth_check
async def cmd_conseil(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text(
            "❌ Usage : `/conseil SYMBOL`\n"
            "_Ex: /conseil EURUSD_\n"
            "_Ex: /conseil BTCUSD_",
            parse_mode="Markdown"
        )
        return

    symbol = context.args[0].upper()
    await update.message.reply_text(f"🔍 Analyse de *{symbol}* en cours...", parse_mode="Markdown")

    try:
        import yfinance as yf
        import numpy as np

        yf_map = {
            "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
            "USDCHF": "USDCHF=X", "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X",
            "NZDUSD": "NZDUSD=X", "EURGBP": "EURGBP=X", "EURJPY": "EURJPY=X",
            "GBPJPY": "GBPJPY=X", "XAUUSD": "GC=F",    "XAGUSD": "SI=F",
            "USOIL":  "CL=F",     "UKOIL":  "BZ=F",     "BTCUSD": "BTC-USD",
            "ETHUSD": "ETH-USD",  "XRPUSD": "XRP-USD",  "LTCUSD": "LTC-USD",
            "BNBUSD": "BNB-USD",  "SOLUSD": "SOL-USD",  "US500":  "^GSPC",
            "US100":  "^NDX",     "GER40":  "^GDAXI",   "UK100":  "^FTSE",
        }

        ticker = yf_map.get(symbol)
        if not ticker:
            await update.message.reply_text(
                f"❌ Symbole *{symbol}* non reconnu.\n"
                f"_Symboles disponibles : {', '.join(yf_map.keys())}_",
                parse_mode="Markdown"
            )
            return

        # Téléchargement données
        df = yf.download(ticker, period="10d", interval="15m", progress=False)
        if df is None or len(df) < 50:
            await update.message.reply_text(f"❌ Pas assez de données pour *{symbol}*.", parse_mode="Markdown")
            return

        df = df[["Close", "High", "Low", "Volume"]].copy()
        df.columns = ["close", "high", "low", "volume"]

        # ── Indicateurs ──
        # EMA
        df["ema20"]  = df["close"].ewm(span=20).mean()
        df["ema50"]  = df["close"].ewm(span=50).mean()
        df["ema200"] = df["close"].ewm(span=200).mean()

        # RSI
        delta = df["close"].diff()
        gain  = delta.where(delta > 0, 0).rolling(14).mean()
        loss  = (-delta.where(delta < 0, 0)).rolling(14).mean().replace(0, 1e-10)
        df["rsi"] = 100 - (100 / (1 + gain / loss))

        # MACD
        ema12 = df["close"].ewm(span=12).mean()
        ema26 = df["close"].ewm(span=26).mean()
        df["macd"]      = ema12 - ema26
        df["macd_sig"]  = df["macd"].ewm(span=9).mean()
        df["macd_hist"] = df["macd"] - df["macd_sig"]

        # ATR (volatilité)
        df["tr"] = pd.concat([
            df["high"] - df["low"],
            (df["high"] - df["close"].shift()).abs(),
            (df["low"]  - df["close"].shift()).abs(),
        ], axis=1).max(axis=1)
        df["atr"] = df["tr"].rolling(14).mean()

        # Bollinger Bands
        df["bb_mid"]   = df["close"].rolling(20).mean()
        df["bb_std"]   = df["close"].rolling(20).std()
        df["bb_upper"] = df["bb_mid"] + 2 * df["bb_std"]
        df["bb_lower"] = df["bb_mid"] - 2 * df["bb_std"]

        last  = df.iloc[-1]
        prev  = df.iloc[-2]
        price = float(last["close"])
        atr   = float(last["atr"])
        rsi   = float(last["rsi"])

        # ── Détermination direction ──
        score_buy  = 0
        score_sell = 0
        signals_detail = []

        # EMA trend
        if last["ema20"] > last["ema50"] > last["ema200"]:
            score_buy += 2
            signals_detail.append("📈 Tendance haussière (EMA 20>50>200)")
        elif last["ema20"] < last["ema50"] < last["ema200"]:
            score_sell += 2
            signals_detail.append("📉 Tendance baissière (EMA 20<50<200)")
        elif last["ema50"] > last["ema200"]:
            score_buy += 1
            signals_detail.append("📈 Tendance moyen terme haussière")
        else:
            score_sell += 1
            signals_detail.append("📉 Tendance moyen terme baissière")

        # RSI
        if rsi < 35:
            score_buy += 2
            signals_detail.append(f"🟢 RSI survendu ({rsi:.1f})")
        elif rsi > 65:
            score_sell += 2
            signals_detail.append(f"🔴 RSI suracheté ({rsi:.1f})")
        elif 40 < rsi < 60:
            signals_detail.append(f"⚪ RSI neutre ({rsi:.1f})")
        elif rsi <= 40:
            score_buy += 1
            signals_detail.append(f"🟡 RSI légèrement survendu ({rsi:.1f})")
        else:
            score_sell += 1
            signals_detail.append(f"🟡 RSI légèrement suracheté ({rsi:.1f})")

        # MACD
        if last["macd_hist"] > 0 and prev["macd_hist"] < last["macd_hist"]:
            score_buy += 2
            signals_detail.append("📈 MACD momentum haussier")
        elif last["macd_hist"] < 0 and prev["macd_hist"] > last["macd_hist"]:
            score_sell += 2
            signals_detail.append("📉 MACD momentum baissier")
        elif last["macd"] > last["macd_sig"]:
            score_buy += 1
            signals_detail.append("📈 MACD au-dessus du signal")
        else:
            score_sell += 1
            signals_detail.append("📉 MACD en-dessous du signal")

        # Bollinger Bands
        if price <= float(last["bb_lower"]) * 1.001:
            score_buy += 1
            signals_detail.append("🟢 Prix proche bande basse Bollinger")
        elif price >= float(last["bb_upper"]) * 0.999:
            score_sell += 1
            signals_detail.append("🔴 Prix proche bande haute Bollinger")

        # ── Score total et direction ──
        total_score = score_buy + score_sell
        confidence  = 0
        direction   = None

        if score_buy > score_sell:
            direction  = "BUY"
            confidence = round((score_buy / max(total_score, 1)) * 100)
        elif score_sell > score_buy:
            direction  = "SELL"
            confidence = round((score_sell / max(total_score, 1)) * 100)
        else:
            direction  = "NEUTRE"
            confidence = 50

        # ── Calcul SL / TP basé sur ATR ──
        # SL = 1.5× ATR, TP = 3× ATR (RR 1:2)
        sl_dist = atr * 1.5
        tp_dist = atr * 3.0

        # Décimales selon le type d'actif
        if symbol in ["USDJPY", "EURJPY", "GBPJPY"]:
            decimals = 3
        elif symbol in ["BTCUSD", "US500", "US100", "GER40", "UK100"]:
            decimals = 2
        elif symbol in ["XAUUSD", "USOIL", "UKOIL", "XAGUSD"]:
            decimals = 2
        elif symbol in ["XRPUSD", "LTCUSD", "BNBUSD", "SOLUSD", "ETHUSD"]:
            decimals = 4
        else:
            decimals = 5

        if direction == "BUY":
            sl = round(price - sl_dist, decimals)
            tp = round(price + tp_dist, decimals)
        elif direction == "SELL":
            sl = round(price + sl_dist, decimals)
            tp = round(price - tp_dist, decimals)
        else:
            sl = round(price - sl_dist, decimals)
            tp = round(price + tp_dist, decimals)

        rr = round(tp_dist / max(sl_dist, 1e-10), 1)

        # ── Conseil LOT selon volatilité ──
        # Volatilité = ATR en % du prix
        volatility_pct = (atr / price) * 100

        if volatility_pct < 0.3:
            lot_conseil = 0.10
            vol_label   = "faible"
        elif volatility_pct < 0.8:
            lot_conseil = 0.05
            vol_label   = "modérée"
        elif volatility_pct < 1.5:
            lot_conseil = 0.02
            vol_label   = "élevée"
        else:
            lot_conseil = 0.01
            vol_label   = "très élevée"

        # ── Construction du message ──
        dir_emoji = "📈" if direction == "BUY" else ("📉" if direction == "SELL" else "➡️")
        conf_bar  = "🟩" * (confidence // 20) + "⬜" * (5 - confidence // 20)

        # Niveau de confiance
        if confidence >= 75:
            conf_label = "Forte"
        elif confidence >= 60:
            conf_label = "Bonne"
        elif confidence >= 50:
            conf_label = "Modérée"
        else:
            conf_label = "Faible"

        signals_txt = "\n".join(f"  {s}" for s in signals_detail)

        msg = (
            f"🎯 *CONSEIL DE TRADING — {symbol}*\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"{dir_emoji} *Direction conseillée : {direction}*\n"
            f"▸ Confiance : {conf_bar} `{confidence}%` ({conf_label})\n\n"
            f"💰 *Paramètres recommandés*\n"
            f"▸ Prix actuel : `{round(price, decimals)}`\n"
            f"▸ Lot conseillé : `{lot_conseil}` _(volatilité {vol_label})_\n"
            f"▸ Stop Loss : `{sl}`\n"
            f"▸ Take Profit : `{tp}`\n"
            f"▸ Ratio R/R : `1:{rr}`\n\n"
            f"📊 *Signaux détectés*\n"
            f"{signals_txt}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        )

        if direction != "NEUTRE" and confidence >= 60:
            cmd_example = f"/{'buy' if direction == 'BUY' else 'sell'} {symbol} {lot_conseil} {sl} {tp}"
            msg += f"✅ *Commande prête à copier :*\n`{cmd_example}`"
        else:
            msg += "⚠️ _Signal trop faible — attends une meilleure opportunité._"

        await update.message.reply_text(msg, parse_mode="Markdown")

    except Exception as e:
        logger.error(f"Erreur conseil {symbol}: {e}")
        await update.message.reply_text(
            f"❌ Erreur lors de l'analyse de *{symbol}* : `{e}`",
            parse_mode="Markdown"
        )

@auth_check
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = session_stats
    winrate = (s["wins"] / s["trades"] * 100) if s["trades"] > 0 else 0
    await update.message.reply_text(
        f"📈 *Statistiques de session*\n\n"
        f"▸ Trades  : `{s['trades']}`\n"
        f"▸ Gagnés  : `{s['wins']}`\n"
        f"▸ Perdus  : `{s['losses']}`\n"
        f"▸ Winrate : `{winrate:.1f}%`\n"
        f"▸ P&L     : `{round(s['profit'], 2)}$`",
        parse_mode="Markdown"
    )

@admin_check
async def cmd_adduser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage : `/adduser TELEGRAM_ID`", parse_mode="Markdown")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID invalide. Doit être un nombre entier.")
        return
    authorized_users.add(uid)
    save_users()
    await update.message.reply_text(f"✅ Utilisateur `{uid}` ajouté.", parse_mode="Markdown")

@admin_check
async def cmd_removeuser(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage : `/removeuser TELEGRAM_ID`", parse_mode="Markdown")
        return
    try:
        uid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID invalide. Doit être un nombre entier.")
        return
    if uid == ADMIN_CHAT_ID:
        await update.message.reply_text("⛔ Tu ne peux pas te retirer toi-même.")
        return
    authorized_users.discard(uid)
    save_users()
    await update.message.reply_text(f"✅ Utilisateur `{uid}` retiré.", parse_mode="Markdown")

@admin_check
async def cmd_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        f"▸ `{uid}`" + (" _(admin)_" if uid == ADMIN_CHAT_ID else "")
        for uid in authorized_users
    ]
    await update.message.reply_text(
        "👥 *Utilisateurs autorisés :*\n\n" + "\n".join(lines),
        parse_mode="Markdown"
    )

# ============================================================
#  CALLBACK BOUTONS — CORRIGÉ (TTL + try/except)
# ============================================================
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_authorized(query.from_user.id):
        await query.edit_message_text("⛔ Accès refusé.")
        return

    # Parsing sécurisé du callback_data
    try:
        action, signal_id = query.data.split("_", 1)
    except ValueError:
        await query.edit_message_text("⚠️ Données de callback invalides.")
        return

    sig = pending_signals.get(signal_id)
    if sig is None:
        await query.edit_message_text("⚠️ Signal expiré ou déjà traité.")
        return

    # Vérification TTL (10 minutes)
    if time.time() - sig.get("ts", 0) > SIGNAL_TTL:
        del pending_signals[signal_id]
        await query.edit_message_text(
            f"⏱️ *Signal {sig['symbol']} expiré* (>{SIGNAL_TTL//60} min).\n"
            "_Lance /signal pour un nouveau signal._",
            parse_mode="Markdown"
        )
        return

    del pending_signals[signal_id]

    if action == "validate":
        await query.edit_message_text(f"⏳ Envoi ordre {sig['direction']} {sig['symbol']}...")
        ok, msg = await place_order(sig["symbol"], sig["direction"], sig["lot"], sig["sl"], sig["tp"])
        await query.edit_message_text(msg, parse_mode="Markdown")
    else:
        await query.edit_message_text(
            f"❌ *Signal {sig['symbol']} refusé.*",
            parse_mode="Markdown"
        )


# ============================================================
#  MONITORING DES TRADES — NOTIF BILAN À LA FERMETURE
# ============================================================

# Stocke les positions ouvertes connues : {ticket: {symbol, direction, lot, open_price, sl, tp, open_time}}
_open_positions: dict[int, dict] = {}

async def _fetch_open_positions() -> dict[int, dict]:
    """Récupère les positions ouvertes actuelles via l'EA."""
    result = await send_command("POSITIONS")
    ok, msg = parse_result(result)
    positions = {}
    if not ok or "NONE" in msg:
        return positions
    for part in msg.split("|"):
        if part.startswith("POS:"):
            fields = part.split(":")[1:]
            if len(fields) < 9:
                continue
            try:
                ticket, sym, direction, vol, popen, pcur, psl, ptp, profit = fields[:9]
                positions[int(ticket)] = {
                    "symbol":     sym,
                    "direction":  direction,
                    "lot":        float(vol),
                    "open_price": float(popen),
                    "cur_price":  float(pcur),
                    "sl":         float(psl),
                    "tp":         float(ptp),
                    "profit":     float(profit),
                }
            except (ValueError, IndexError):
                continue
    return positions

async def monitor_trades(app):
    """
    Vérifie toutes les 10s si une position s'est fermée.
    Quand une position disparaît → envoie un bilan Telegram.
    """
    global _open_positions, session_stats

    # Attente initiale pour laisser le bot démarrer
    await asyncio.sleep(15)

    # Initialisation : snapshot des positions actuelles
    _open_positions = await _fetch_open_positions()
    logger.info(f"📊 Monitor trades démarré — {len(_open_positions)} position(s) en cours")

    while True:
        await asyncio.sleep(10)  # Vérifie toutes les 10 secondes
        try:
            current = await _fetch_open_positions()

            # Détection des positions fermées (étaient dans _open_positions, plus dans current)
            closed_tickets = set(_open_positions.keys()) - set(current.keys())

            for ticket in closed_tickets:
                pos = _open_positions[ticket]
                profit = pos["profit"]
                symbol = pos["symbol"]
                direction = pos["direction"]
                lot = pos["lot"]
                open_price = pos["open_price"]

                # Mise à jour des stats
                session_stats["trades"] += 1
                session_stats["profit"] = round(session_stats["profit"] + profit, 2)
                if profit >= 0:
                    session_stats["wins"] += 1
                    result_emoji = "✅"
                    result_label = "GAGNANT"
                else:
                    session_stats["losses"] += 1
                    result_emoji = "❌"
                    result_label = "PERDANT"
                save_stats()

                # Winrate global
                total = session_stats["trades"]
                winrate = round((session_stats["wins"] / total) * 100, 1) if total > 0 else 0

                # Direction emoji
                dir_emoji = "📈" if direction == "BUY" else "📉"

                # Bilan message
                bilan = (
                    f"{result_emoji} *TRADE FERMÉ — {result_label}*\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"{dir_emoji} *{direction} {symbol}*\n\n"
                    f"▸ Ticket      : `#{ticket}`\n"
                    f"▸ Lot         : `{lot}`\n"
                    f"▸ Prix ouvert : `{open_price}`\n"
                    f"▸ Prix fermé  : `{pos['cur_price']}`\n"
                    f"▸ P&L         : `{'+ ' if profit >= 0 else ''}{round(profit, 2)}$`\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📊 *Statistiques de session*\n"
                    f"▸ Trades  : `{session_stats['trades']}`\n"
                    f"▸ Gagnés  : `{session_stats['wins']}` | Perdus : `{session_stats['losses']}`\n"
                    f"▸ Winrate : `{winrate}%`\n"
                    f"▸ P&L total : `{'+ ' if session_stats['profit'] >= 0 else ''}{session_stats['profit']}$`"
                )

                await app.bot.send_message(
                    chat_id=ADMIN_CHAT_ID,
                    text=bilan,
                    parse_mode="Markdown"
                )
                logger.info(f"📬 Bilan envoyé — ticket #{ticket} | P&L: {profit}$")

            # Détection des nouvelles positions ouvertes
            new_tickets = set(current.keys()) - set(_open_positions.keys())
            for ticket in new_tickets:
                pos = current[ticket]
                logger.info(f"📌 Nouvelle position détectée — #{ticket} {pos['direction']} {pos['symbol']}")

            # Mise à jour du snapshot
            _open_positions = current

        except Exception as e:
            logger.error(f"Erreur monitor trades: {e}")

# ============================================================
#  NETTOYAGE DES SIGNAUX EXPIRÉS
# ============================================================
async def cleanup_signals():
    """Supprime les signaux expirés toutes les 5 minutes."""
    while True:
        await asyncio.sleep(300)
        now = time.time()
        expired = [sid for sid, sig in pending_signals.items()
                   if now - sig.get("ts", 0) > SIGNAL_TTL]
        for sid in expired:
            del pending_signals[sid]
        if expired:
            logger.info(f"🧹 {len(expired)} signal(s) expiré(s) supprimé(s)")

# ============================================================
#  SCAN AUTO AVEC BACK-OFF
# ============================================================
async def auto_signal_scan(app):
    consecutive_errors = 0
    while True:
        # Back-off exponentiel en cas d'erreurs réseau
        interval = min(SCAN_INTERVAL * (2 ** consecutive_errors), 1800)
        await asyncio.sleep(interval)
        errors_this_round = 0

        for symbol in SYMBOLS:
            try:
                sig = get_signal(symbol)
                if sig:
                    signal_id = f"{symbol}_{int(time.time())}"
                    pending_signals[signal_id] = sig
                    text, keyboard = format_signal_message(sig, signal_id)
                    await app.bot.send_message(
                        chat_id=ADMIN_CHAT_ID,
                        text=text,
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                    )
            except Exception as e:
                logger.error(f"Erreur scan auto {symbol}: {e}")
                errors_this_round += 1

        if errors_this_round >= len(SYMBOLS):
            consecutive_errors += 1
            logger.warning(f"Back-off activé : prochain scan dans {min(SCAN_INTERVAL * (2 ** consecutive_errors), 1800)}s")
        else:
            consecutive_errors = 0

# ============================================================
#  MAIN
# ============================================================
async def post_init(app):
    load_users()
    load_stats()
    asyncio.create_task(auto_signal_scan(app))
    asyncio.create_task(cleanup_signals())
    asyncio.create_task(monitor_trades(app))
    await app.bot.send_message(
        chat_id=ADMIN_CHAT_ID,
        text=(
            "🤖 *Bot MT5 démarré !*\n\n"
            f"▸ Symboles  : `{', '.join(SYMBOLS)}`\n"
            f"▸ Lot défaut : `{DEFAULT_LOT}`\n"
            f"▸ Timeout EA : `{CMD_TIMEOUT}s`\n\n"
            "Tape /start pour voir toutes les commandes.\n"
            "_⚠️ Assure-toi que l'EA TelegramBridge tourne dans MT5 !_"
        ),
        parse_mode="Markdown",
    )
    logger.info("✅ Bot MT5 lancé.")

def main():
    if not TELEGRAM_TOKEN or TELEGRAM_TOKEN == "METS_TON_TOKEN_ICI":
        logger.error("❌ TELEGRAM_TOKEN non configuré ! Crée un fichier .env")
        return

    if ADMIN_CHAT_ID == 0:
        logger.error("❌ ADMIN_CHAT_ID non configuré ! Crée un fichier .env")
        return

    if not os.path.exists(MT5_COMMON):
        logger.warning(f"⚠️ Dossier MT5 introuvable : {MT5_COMMON}")
        logger.warning("    → Configure MT5_COMMON_PATH dans .env si nécessaire")

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("solde",      cmd_solde))
    app.add_handler(CommandHandler("positions",  cmd_positions))
    app.add_handler(CommandHandler("buy",        cmd_buy))
    app.add_handler(CommandHandler("sell",       cmd_sell))
    app.add_handler(CommandHandler("close",      cmd_close))
    app.add_handler(CommandHandler("closeall",   cmd_closeall))
    app.add_handler(CommandHandler("signal",     cmd_signal))
    app.add_handler(CommandHandler("conseil",    cmd_conseil))
    app.add_handler(CommandHandler("stats",      cmd_stats))
    app.add_handler(CommandHandler("adduser",    cmd_adduser))
    app.add_handler(CommandHandler("removeuser", cmd_removeuser))
    app.add_handler(CommandHandler("users",      cmd_users))
    app.add_handler(CallbackQueryHandler(handle_callback))

    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()