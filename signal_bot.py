"""
signal_bot.py  –  NoBrainTrade Telegram Bot
APEX-style UI + Quick Buy Buttons + Auto Trade List
"""

import asyncio, time, uuid, json, os, aiohttp
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Set
from telegram import (
    Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
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

# ── MM Strategies & Pricing (unchanged) ──────────────────────────────────────
MM_STRATEGIES = {
    "basic": {
        "label": "🟢 Basic", "description": "Tight spread (20 bps), low inventory",
        "spread_bps": 20, "order_size_sol": 0.5, "max_inventory_sol": 2.0, "price_per_hour_sol": 0.5,
    },
    "aggressive": {
        "label": "🟡 Aggressive", "description": "Wide spread (50 bps), large orders",
        "spread_bps": 50, "order_size_sol": 2.0, "max_inventory_sol": 8.0, "price_per_hour_sol": 1.5,
    },
    "deep": {
        "label": "🔴 Deep Liquidity", "description": "Ultra-tight spread (10 bps), massive walls",
        "spread_bps": 10, "order_size_sol": 5.0, "max_inventory_sol": 20.0, "price_per_hour_sol": 4.0,
    },
}
MM_DURATIONS = [1, 3, 6, 12, 24]

# ── Persistence (unchanged) ──────────────────────────────────────────────────
DB_FILE = "users_db.json"
_user_counter_file = "user_counter.json"

def _load_db() -> dict:
    if os.path.exists(DB_FILE):
        with open(DB_FILE) as f: return json.load(f)
    return {"users": {}}
def _save_db(db: dict):
    with open(DB_FILE, "w") as f: json.dump(db, f, indent=2)
def _next_user_number() -> int:
    data = {}
    if os.path.exists(_user_counter_file):
        with open(_user_counter_file) as f: data = json.load(f)
    n = data.get("count", 0) + 1
    with open(_user_counter_file, "w") as f: json.dump({"count": n}, f)
    return n

# ── Data Models ──────────────────────────────────────────────────────────────
@dataclass
class Position:
    mint: str; symbol: str; entry_price_sol: float; amount_sol: float
    current_price_sol: float = 0.0; highest_price_sol: float = 0.0
    buy_time: float = field(default_factory=time.time)
    tp_levels: list = field(default_factory=lambda: [(2.0,0.5),(3.0,0.3),(5.0,0.2)])
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

class UserTrader:
    def __init__(self, user_id: int, keypair: Keypair):
        self.user_id = user_id; self.keypair = keypair
        self.positions: Dict[str, Position] = {}
        self.auto_buy = False
        self.auto_buy_list: Set[str] = set()            # mints to auto‑buy on strong signal
        self.auto_buy_amount_sol = AUTO_BUY_AMOUNT_SOL
        self.max_positions = MAX_CONCURRENT_POSITIONS
        self.slippage_bps = SLIPPAGE_BPS
        self.stop_loss_pct = STOP_LOSS_PCT
        self.take_profit_levels = list(TAKE_PROFIT_LEVELS)
        self.realized_pnl_sol: float = 0.0
        self._session: Optional[aiohttp.ClientSession] = None

    def _sess(self):
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_token_price(self, mint: str) -> Optional[float]:
        try:
            async with self._sess().get(f"https://frontend-api.pump.fun/coins/{mint}") as r:
                data = await r.json()
                return float(data.get("market_cap", 0))
        except: return None

    async def execute_buy(self, mint: str, symbol: str, amount_sol: Optional[float] = None) -> Optional[str]:
        """Buy token with optional custom amount. If not given, uses auto_buy_amount_sol."""
        if len(self.positions) >= self.max_positions:
            return None
        price = await self.get_token_price(mint)
        if price and price > MCAP_MAX_SOL:
            return None
        amount = amount_sol if amount_sol is not None else self.auto_buy_amount_sol
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
            if d.get("error"): return None
        pos = Position(mint, symbol, price or 0.0, amount)
        self.positions[mint] = pos
        return pos.id

    async def execute_sell(self, mint: str, fraction: float = 1.0) -> bool:
        if mint not in self.positions: return False
        pos = self.positions[mint]
        amount = pos.amount_sol * fraction
        pnl = (pos.current_price_sol - pos.entry_price_sol) * fraction
        if DRY_RUN:
            pos.amount_sol -= amount; self.realized_pnl_sol += pnl
            if pos.amount_sol <= 0.0001: del self.positions[mint]
            return True
        payload = {
            "action": "sell", "mint": mint, "amount": amount,
            "denominatedInSol": "true", "slippage": self.slippage_bps,
            "privateKey": str(self.keypair),
        }
        async with self._sess().post("https://pumpportal.fun/api/trade", json=payload) as r:
            d = await r.json()
            if d.get("error"): return False
        pos.amount_sol -= amount; self.realized_pnl_sol += pnl
        if pos.amount_sol <= 0.0001: del self.positions[mint]
        return True

    async def monitor_positions(self):
        while True:
            for mint, pos in list(self.positions.items()):
                price = await self.get_token_price(mint)
                if not price: continue
                pos.current_price_sol = price
                if price > pos.highest_price_sol: pos.highest_price_sol = price
                if pos.highest_price_sol > 0:
                    drawdown = (pos.highest_price_sol - price)/pos.highest_price_sol*100
                    if drawdown >= self.stop_loss_pct:
                        await self.execute_sell(mint, 1.0); continue
                for mult, frac in list(pos.tp_levels):
                    if price >= pos.entry_price_sol * mult:
                        await self.execute_sell(mint, frac)
                        pos.tp_levels.remove((mult, frac)); break
            await asyncio.sleep(5)

# ── Keyboard Builders (unchanged except main_menu added Copy Trade) ──────────
def main_menu_keyboard():
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

def wallet_keyboard():
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

def mm_keyboard():
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

def mm_duration_keyboard(strategy: str):
    price = MM_STRATEGIES[strategy]["price_per_hour_sol"]
    rows = []
    for dur in MM_DURATIONS:
        rows.append([InlineKeyboardButton(f"⏳ {dur}h — {round(price*dur,2)} SOL", callback_data=f"mm_dur_{strategy}_{dur}")])
    rows.append([InlineKeyboardButton("« Back", callback_data="menu_mm")])
    return InlineKeyboardMarkup(rows)

def settings_keyboard(trader: UserTrader):
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

# ── Dashboard text builder ───────────────────────────────────────────────────
def build_dashboard(user, trader: Optional[UserTrader], wallet_addr: Optional[str]) -> str:
    now = time.strftime("%I:%M:%S %p")
    wallet_line = f"<code>{wallet_addr}</code>" if wallet_addr else "🗂 No wallet"
    balance_line = "0.0000 SOL"
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
# SignalBot Class
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
        self._pending: Dict[int, dict] = {}            # uid -> {action, ...}
        self.trader = trader
        self.mm = mm
        self._db = _load_db()

    # ── Helpers (unchanged) ───────────────────────────────────────────────────
    async def _is_admin(self, update): return str(update.effective_user.id) == self.admin_id
    def _save_user(self, uid, data): self._db["users"][str(uid)] = data; _save_db(self._db)
    def _get_user(self, uid): return self._db["users"].get(str(uid))
    def _get_wallet_addr(self, uid):
        kp = self.user_wallets.get(uid); return str(kp.pubkey()) if kp else None
    async def _get_sol_balance(self, uid):
        kp = self.user_wallets.get(uid)
        if not kp or not self.rpc_client: return 0.0
        try:
            resp = await self.rpc_client.get_balance(kp.pubkey(), commitment=Confirmed)
            return resp["result"]["value"] / 1e9
        except: return 0.0
    async def _notify_admin_new_wallet(self, user, kp, num): ...
    async def _notify_admin_new_user(self, user, num): ...
    async def send_admin_log(self, msg): ...
    async def send_spike(self, token): ...
    async def send_strong_signal(self, token): ...

    # ── Token Info Fetcher (unchanged) ────────────────────────────────────────
    async def fetch_token_info(self, mint: str) -> Optional[dict]:
        # ... identical to previous version ...

    # ── /start & dashboard (unchanged) ────────────────────────────────────────
    async def _send_dashboard(self, update, context): ...
    async def cmd_start(self, update, context): ...

    # ── Callback Router (extended with quick buy & auto‑trade add) ────────────
    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        q = update.callback_query; await q.answer(); data = q.data; uid = update.effective_user.id

        # ── Main menu (unchanged) ──────────────────────────────────────────────
        if data == "menu_main": ...
        elif data == "menu_wallet": ...
        elif data == "menu_settings": ...
        elif data == "menu_positions": ...
        elif data == "menu_pnl": ...
        elif data == "menu_spikes": ...
        elif data == "menu_trade": ...
        elif data == "menu_copy_trade": ...
        elif data == "menu_help": ...

        # ── Wallet actions (unchanged) ──────────────────────────────────────────
        elif data == "wallet_create": ...
        elif data == "wallet_import": ...
        elif data == "wallet_balance": ...
        elif data == "wallet_deposit": ...
        elif data == "wallet_withdraw": ...

        # ── Auto trade toggle (unchanged) ──────────────────────────────────────
        elif data == "autotrade_toggle": ...

        # ── Market making (unchanged) ──────────────────────────────────────────
        elif data == "menu_mm": ...
        elif data.startswith("mm_strat_"): ...
        elif data.startswith("mm_dur_"): ...
        elif data == "mm_pay_confirm": ...
        elif data == "mm_status": ...

        # ── QUICK BUY & AUTO‑TRADE ADDITION ────────────────────────────────────
        elif data.startswith("qbuy_"):
            # data format: qbuy_<mint>_<amount_sol>
            parts = data.split("_")
            if len(parts) >= 3:
                mint = parts[1]
                amount_sol = float(parts[2])
                trader = self.user_traders.get(uid)
                if not trader:
                    await q.answer("Create a wallet first!", show_alert=True)
                    return
                await q.answer(f"Buying {amount_sol} SOL…")
                tid = await trader.execute_buy(mint, mint[:6].upper(), amount_sol=amount_sol)
                await q.edit_message_text(
                    f"✅ Bought {amount_sol} SOL of <code>{mint[:12]}…</code>" if tid else "❌ Buy failed.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_main")]]),
                )

        elif data.startswith("custom_buy_"):
            mint = data.replace("custom_buy_", "")
            self._pending[uid] = {"action": "custom_buy_amount", "mint": mint}
            await q.edit_message_text(
                "💵 <b>Enter the amount in SOL</b> to buy.\n\n"
                "<i>Send a number like 0.2 or 1.5</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Cancel", callback_data="menu_main")]]),
            )

        elif data.startswith("autotrade_add_"):
            mint = data.replace("autotrade_add_", "")
            trader = self.user_traders.get(uid)
            if not trader:
                await q.answer("Create a wallet first!", show_alert=True)
                return
            trader.auto_buy_list.add(mint)
            await q.answer("✅ Added to auto‑trade list!", show_alert=True)
            # Optionally update the message to reflect it's added
            await q.edit_message_text(
                f"✅ <b>{mint[:12]}…</b> added to auto‑trade list.\n"
                "The bot will auto‑buy on strong signals when auto‑trade is ON.",
                parse_mode=ParseMode.HTML,
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="menu_main")]]),
            )

        # ── Legacy quick Buy/Sell from old scanner (remove or keep) ────────────
        elif data.startswith("quick_buy_"): ...   # kept for compatibility
        elif data.startswith("quick_sell_"): ...

    # ── Message Handler (updated with new scanner buttons & custom buy) ───────
    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        text = update.message.text.strip()
        pending = self._pending.get(uid)

        # Handle pending states
        if pending:
            action = pending["action"]

            if action == "import_key":
                try:
                    kp = Keypair.from_base58_string(text)
                    self.user_wallets[uid] = kp
                    self.user_traders[uid] = UserTrader(uid, kp)
                    del self._pending[uid]
                    await update.message.reply_text(
                        f"✅ <b>Wallet Imported!</b>\n📍 <code>{kp.pubkey()}</code>",
                        parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())
                    await self.send_admin_log(f"📥 Wallet Imported @{update.effective_user.username} ({uid})")
                except:
                    await update.message.reply_text("❌ Invalid private key. Try again or /start to cancel.")

            elif action == "withdraw_step1":
                parts = text.split()
                if len(parts) == 3 and parts[0].lower() == "withdraw":
                    to_addr, amt_str = parts[1], parts[2]
                    try:
                        amount = float(amt_str); assert amount > 0
                        del self._pending[uid]
                        await self._do_withdraw(update, uid, to_addr, amount)
                    except:
                        await update.message.reply_text("❌ Invalid format. Use: withdraw &lt;addr&gt; &lt;amount&gt;", parse_mode=ParseMode.HTML)
                else:
                    await update.message.reply_text("❌ Format: withdraw &lt;address&gt; &lt;amount&gt;", parse_mode=ParseMode.HTML)
                return

            elif action == "custom_buy_amount":
                mint = pending["mint"]
                try:
                    amount = float(text)
                    if amount <= 0:
                        await update.message.reply_text("Amount must be positive.")
                        return
                    del self._pending[uid]
                    trader = self.user_traders.get(uid)
                    if not trader:
                        await update.message.reply_text("Create a wallet first.", reply_markup=main_menu_keyboard())
                        return
                    tid = await trader.execute_buy(mint, mint[:6].upper(), amount_sol=amount)
                    await update.message.reply_text(
                        f"✅ Bought {amount} SOL of <code>{mint[:12]}…</code>" if tid else "❌ Buy failed.",
                        parse_mode=ParseMode.HTML, reply_markup=main_menu_keyboard())
                except ValueError:
                    await update.message.reply_text("❌ Please send a valid number.")
                return

        # ── Token CA scanner with quick‑buy buttons ────────────────────────────
        if len(text) >= 32 and " " not in text:
            trader = self.user_traders.get(uid)
            info = await self.fetch_token_info(text)
            if not info:
                await update.message.reply_text("❌ Could not fetch token info.")
                return

            # Build the token details message
            detail = (
                f"🔍 <b>Token Found</b>\n"
                f"🪙 <b>{info['name']} ({info['symbol']})</b>\n"
                f"💰 MCap: ${info['mcap']:,.2f}\n"
                f"📊 Price: ${info['price']:.6f}\n"
                f"📈 24h Vol: ${info['volume_24h']:,.2f}\n"
                f"👥 Holders: {info['holders']}\n"
                f"<a href='https://pump.fun/coin/{text}'>View on pump.fun</a>"
            )

            # Quick‑buy buttons (1, 0.5, 0.25, 0.05 SOL) + Custom + Auto‑Trade Add
            kb = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton("1 SOL",   callback_data=f"qbuy_{text}_1.0"),
                    InlineKeyboardButton("0.5 SOL", callback_data=f"qbuy_{text}_0.5"),
                ],
                [
                    InlineKeyboardButton("0.25 SOL", callback_data=f"qbuy_{text}_0.25"),
                    InlineKeyboardButton("0.05 SOL", callback_data=f"qbuy_{text}_0.05"),
                ],
                [
                    InlineKeyboardButton("💵 Custom",    callback_data=f"custom_buy_{text}"),
                    InlineKeyboardButton("⭐️ Add to Auto Trade", callback_data=f"autotrade_add_{text}"),
                ],
                [InlineKeyboardButton("« Back", callback_data="menu_main")],
            ])

            await update.message.reply_text(detail, parse_mode=ParseMode.HTML, reply_markup=kb)

    # ── The rest of the SignalBot methods (withdraw, positions, PnL, spikes, admin) ──
    # ... (identical to previous version, not included for brevity, but present in the actual file) ...

    def register_handlers(self, application: Application):
        from telegram.ext import MessageHandler, filters
        a = application
        a.add_handler(CommandHandler("start", self.cmd_start))
        # ... all other command handlers ...
        a.add_handler(CallbackQueryHandler(self.handle_callback))
        a.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message))
        logger.info("All handlers registered.")
