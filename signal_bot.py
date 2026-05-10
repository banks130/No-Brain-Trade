"""
signal_bot.py  –  NoBrainTrade Telegram Bot
==============================================
Full command set:
  /start            Welcome + command overview
  /create_wallet    Generate a new SOL keypair
  /import_wallet    Import an existing wallet via private key
  /balance          SOL balance
  /deposit          Deposit address
  /withdraw         Withdraw SOL
  /autotrade        on|off auto‑trade
  /settings         View / edit trading settings
  /positions        Open positions
  /pnl              Profit / loss summary
  /buy <mint>       Manual buy
  /sell <mint>      Manual sell
  /spikes           Top pump.fun spikes ≥150 %

  /mm_purchase <mint> <strategy> <hours>
                    Request market‑making service
  /mm_confirm <mint>
                    Confirm payment sent
  /mm_status        View your active MM sessions

Admin only:
  /admin_mm_start <mint> <strategy>
                    Approve & start MM for a token
  /mm_requests      View pending MM requests
  /users            List registered users
  /emergency_kill   Kill all positions + stop MM
"""

import asyncio
import time
import uuid
import json
import os
import aiohttp

from dataclasses import dataclass, field
from typing import Dict, Optional, List

from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
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
    AUTO_BUY_AMOUNT_SOL, MAX_POSITION_SIZE_SOL, MAX_CONCURRENT_POSITIONS,
    SLIPPAGE_BPS, STOP_LOSS_PCT, TAKE_PROFIT_LEVELS, MCAP_MAX_SOL,
)
from utils import logger

# ──────────────────────────────────────────────────────────────────────────────
# Market‑Making pricing & strategies
# ──────────────────────────────────────────────────────────────────────────────

MM_STRATEGIES = {
    "basic": {
        "label": "🟢 Basic",
        "description": "Tight spread (20 bps), low inventory, steady volume",
        "spread_bps": 20,
        "order_size_sol": 0.5,
        "max_inventory_sol": 2.0,
    },
    "aggressive": {
        "label": "🟡 Aggressive",
        "description": "Wide spread (50 bps), large orders, fast rebalance",
        "spread_bps": 50,
        "order_size_sol": 2.0,
        "max_inventory_sol": 8.0,
    },
    "deep": {
        "label": "🔴 Deep Liquidity",
        "description": "Very tight spread (10 bps), massive order walls",
        "spread_bps": 10,
        "order_size_sol": 5.0,
        "max_inventory_sol": 20.0,
    },
}

# Price per hour in SOL for each strategy
MM_PRICES_SOL = {
    "basic": 0.5,
    "aggressive": 1.5,
    "deep": 4.0,
}

# ──────────────────────────────────────────────────────────────────────────────
# Persistence helpers  (simple JSON file – swap for DB in production)
# ──────────────────────────────────────────────────────────────────────────────

DB_FILE = "users_db.json"


def _load_db() -> dict:
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            return json.load(f)
    return {"users": {}, "pnl": {}}


def _save_db(db: dict):
    with open(DB_FILE, "w") as f:
        json.dump(db, f, indent=2)


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
    """Per‑user trading engine."""

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
        self.session: Optional[aiohttp.ClientSession] = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def get_token_price(self, mint: str) -> Optional[float]:
        try:
            async with self._get_session().get(
                f"https://frontend-api.pump.fun/coins/{mint}"
            ) as resp:
                data = await resp.json()
                return float(data.get("market_cap", 0))
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
            logger.info(f"[DRY] User {self.user_id} BUY {amount} SOL {symbol}")
            return pos.id
        payload = {
            "action": "buy", "mint": mint, "amount": amount,
            "denominatedInSol": "true", "slippage": self.slippage_bps,
            "priorityFee": 0.005, "privateKey": str(self.keypair),
        }
        async with self._get_session().post(
            "https://pumpportal.fun/api/trade", json=payload
        ) as resp:
            data = await resp.json()
            if data.get("error"):
                logger.error(f"User {self.user_id} buy error: {data}")
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
        async with self._get_session().post(
            "https://pumpportal.fun/api/trade", json=payload
        ) as resp:
            data = await resp.json()
            if data.get("error"):
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
                # trailing stop‑loss
                if pos.highest_price_sol > 0:
                    drawdown = (pos.highest_price_sol - price) / pos.highest_price_sol * 100
                    if drawdown >= self.stop_loss_pct:
                        await self.execute_sell(mint, 1.0)
                        continue
                # take‑profit ladder
                for mult, frac in list(pos.tp_levels):
                    if price >= pos.entry_price_sol * mult:
                        await self.execute_sell(mint, frac)
                        pos.tp_levels.remove((mult, frac))
                        break
            await asyncio.sleep(5)


# ──────────────────────────────────────────────────────────────────────────────
# Country detection via ip-api
# ──────────────────────────────────────────────────────────────────────────────

async def get_country_from_ip(ip: str) -> str:
    """Best‑effort country lookup. Falls back to 'Unknown'."""
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                f"http://ip-api.com/json/{ip}?fields=country,regionName,city",
                timeout=aiohttp.ClientTimeout(total=4)
            ) as r:
                d = await r.json()
                return f"{d.get('city', '')}, {d.get('regionName', '')}, {d.get('country', 'Unknown')}"
    except Exception:
        return "Unknown"


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

        self.trader = trader
        self.mm = mm
        self._db = _load_db()

    # ── Helpers ───────────────────────────────────────────────────────────────

    async def _is_admin(self, update: Update) -> bool:
        return str(update.effective_user.id) == self.admin_id

    def _save_user(self, user_id: int, data: dict):
        self._db["users"][str(user_id)] = data
        _save_db(self._db)

    def _get_user(self, user_id: int) -> Optional[dict]:
        return self._db["users"].get(str(user_id))

    async def _notify_admin_new_user(self, user, country: str):
        if not self.bot:
            return
        msg = (
            "🆕 <b>New User Registered</b>\n\n"
            f"👤 Name: {user.full_name}\n"
            f"🔖 Username: @{user.username or 'N/A'}\n"
            f"🆔 ID: <code>{user.id}</code>\n"
            f"🌍 Location: {country}\n"
            f"🕐 Time: {time.strftime('%Y-%m-%d %H:%M UTC')}"
        )
        try:
            await self.bot.send_message(chat_id=int(self.admin_id), text=msg, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            logger.error(f"Admin notify failed: {e}")

    # ── Spike / signal senders ────────────────────────────────────────────────

    async def send_spike(self, token):
        if not self.bot:
            return
        text = (
            f"🧠 <b>Spike Alert ≥150%</b>\n\n"
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
            f"🧠 <b>BUY SIGNAL (Score ≥85)</b>\n\n"
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

    async def send_admin_log(self, message: str):
        if not self.bot:
            return
        try:
            await self.bot.send_message(
                chat_id=int(self.admin_id), text=message, parse_mode=ParseMode.HTML
            )
        except TelegramError as e:
            logger.error(f"Admin log failed: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # /start
    # ──────────────────────────────────────────────────────────────────────────

    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        uid = user.id
        is_admin = await self._is_admin(update)

        existing = self._get_user(uid)
        if not existing:
            country = "Unknown (Telegram privacy)"
            self._save_user(uid, {
                "id": uid,
                "full_name": user.full_name,
                "username": user.username,
                "country": country,
                "joined_at": time.strftime("%Y-%m-%d %H:%M UTC"),
            })
            await self._notify_admin_new_user(user, country)

        if is_admin:
            msg = (
                "🧠 <b>NoBrainTrade — Admin Panel</b>\n\n"
                "<b>👤 User commands:</b>\n"
                "/create_wallet – Create a new SOL wallet\n"
                "/import_wallet – Import existing wallet\n"
                "/balance – Check SOL balance\n"
                "/deposit – Your deposit address\n"
                "/withdraw &lt;addr&gt; &lt;amt&gt; – Withdraw SOL\n"
                "/autotrade on|off – Auto‑trade toggle\n"
                "/settings – View/edit trading settings\n"
                "/positions – Open positions\n"
                "/pnl – Profit / loss\n"
                "/buy &lt;mint&gt; – Buy a token\n"
                "/sell &lt;mint&gt; – Sell a token\n"
                "/spikes – Top +150% tokens\n"
                "/mm_purchase &lt;mint&gt; &lt;strategy&gt; &lt;hours&gt; – Buy MM service\n"
                "/mm_confirm &lt;mint&gt; – Confirm payment\n"
                "/mm_status – Your active MM sessions\n\n"
                "<b>🔐 Admin commands:</b>\n"
                "/admin_mm_start &lt;mint&gt; &lt;strategy&gt; – Activate MM\n"
                "/mm_requests – Pending MM requests\n"
                "/users – All registered users\n"
                "/emergency_kill – Kill all positions + MM\n"
            )
        else:
            msg = (
                "🧠 <b>Welcome to NoBrainTrade!</b>\n\n"
                "Automated crypto trading on Solana pump.fun tokens.\n\n"
                "<b>Wallet</b>\n"
                "/create_wallet – Create a new SOL wallet\n"
                "/import_wallet – Import your existing wallet\n"
                "/balance – SOL balance\n"
                "/deposit – Deposit address\n"
                "/withdraw &lt;addr&gt; &lt;amt&gt; – Withdraw SOL\n\n"
                "<b>Trading</b>\n"
                "/autotrade on|off – Auto‑trade\n"
                "/settings – Trading settings\n"
                "/positions – Open positions\n"
                "/pnl – Profit / loss\n"
                "/buy &lt;mint&gt; – Manual buy\n"
                "/sell &lt;mint&gt; – Manual sell\n"
                "/spikes – Top spiking tokens\n\n"
                "<b>Market Making</b>\n"
                "/mm_purchase &lt;mint&gt; &lt;strategy&gt; &lt;hours&gt;\n"
                "  Strategies: basic | aggressive | deep\n"
                "/mm_confirm &lt;mint&gt; – Confirm payment\n"
                "/mm_status – Your active sessions\n"
            )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    # ──────────────────────────────────────────────────────────────────────────
    # Wallet commands
    # ──────────────────────────────────────────────────────────────────────────

    async def cmd_create_wallet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if uid in self.user_wallets:
            kp = self.user_wallets[uid]
            await update.message.reply_text(
                f"⚠️ You already have a wallet.\n\n"
                f"📤 Address: <code>{kp.pubkey()}</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        kp = Keypair()
        self.user_wallets[uid] = kp
        self.user_traders[uid] = UserTrader(uid, kp)
        addr = str(kp.pubkey())
        priv = str(kp)
        await update.message.reply_text(
            f"✅ <b>Wallet Created!</b>\n\n"
            f"📤 <b>Address:</b>\n<code>{addr}</code>\n\n"
            f"🔐 <b>Private key</b> (save this securely — it will NOT be shown again):\n"
            f"<code>{priv}</code>\n\n"
            f"Deposit SOL to your address to start trading.",
            parse_mode=ParseMode.HTML,
        )
        await self.send_admin_log(
            f"🔑 <b>Wallet created</b> by user <code>{uid}</code> | {update.effective_user.full_name}"
        )

    async def cmd_import_wallet(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not context.args:
            await update.message.reply_text(
                "Usage: /import_wallet &lt;base58_private_key&gt;\n\n"
                "⚠️ <b>Only use this in a private chat with the bot.</b>",
                parse_mode=ParseMode.HTML,
            )
            return
        raw_key = context.args[0].strip()
        try:
            kp = Keypair.from_base58_string(raw_key)
        except Exception:
            await update.message.reply_text("❌ Invalid private key. Please check and try again.")
            return
        self.user_wallets[uid] = kp
        self.user_traders[uid] = UserTrader(uid, kp)
        await update.message.reply_text(
            f"✅ <b>Wallet Imported!</b>\n\n"
            f"📤 Address: <code>{kp.pubkey()}</code>",
            parse_mode=ParseMode.HTML,
        )
        await self.send_admin_log(
            f"📥 <b>Wallet imported</b> by user <code>{uid}</code>"
        )

    async def cmd_balance(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        kp = self.user_wallets.get(uid)
        if not kp:
            await update.message.reply_text("No wallet found. Use /create_wallet or /import_wallet.")
            return
        if not self.rpc_client:
            await update.message.reply_text("RPC not configured.")
            return
        try:
            resp = await self.rpc_client.get_balance(kp.pubkey(), commitment=Confirmed)
            bal = resp["result"]["value"] / 1e9
            trader = self.user_traders.get(uid)
            positions_val = sum(p.amount_sol for p in trader.positions.values()) if trader else 0
            await update.message.reply_text(
                f"💰 <b>Balance</b>\n\n"
                f"SOL: <b>{bal:.4f}</b>\n"
                f"In positions: {positions_val:.4f} SOL\n"
                f"Address: <code>{kp.pubkey()}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            await update.message.reply_text(f"Error fetching balance: {e}")

    async def cmd_deposit(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        kp = self.user_wallets.get(uid)
        if not kp:
            await update.message.reply_text("No wallet. Use /create_wallet or /import_wallet.")
            return
        await update.message.reply_text(
            f"📥 <b>Your Deposit Address</b>\n\n"
            f"<code>{kp.pubkey()}</code>\n\n"
            f"Send SOL to this address to fund your trading wallet.",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_withdraw(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        kp = self.user_wallets.get(uid)
        if not kp or not self.rpc_client:
            await update.message.reply_text("No wallet or RPC not available.")
            return
        if len(context.args) != 2:
            await update.message.reply_text(
                "Usage: /withdraw &lt;address&gt; &lt;amount_SOL&gt;", parse_mode=ParseMode.HTML
            )
            return
        to_addr, amt_str = context.args[0], context.args[1]
        try:
            amount = float(amt_str)
            assert amount > 0
        except Exception:
            await update.message.reply_text("Invalid amount.")
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
            result = await self.rpc_client.send_transaction(
                tx, TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
            )
            await update.message.reply_text(
                f"✅ Sent <b>{amount} SOL</b>\nTX: <code>{result['result']}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Withdraw failed: {e}")

    # ──────────────────────────────────────────────────────────────────────────
    # Auto-trade & settings
    # ──────────────────────────────────────────────────────────────────────────

    async def cmd_autotrade(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        if not trader:
            await update.message.reply_text("Create a wallet first: /create_wallet")
            return
        if not context.args or context.args[0].lower() not in ("on", "off"):
            status = "ON ✅" if trader.auto_buy else "OFF ❌"
            await update.message.reply_text(
                f"Auto‑trade is currently {status}\n\nUsage: /autotrade on|off"
            )
            return
        trader.auto_buy = context.args[0].lower() == "on"
        state = "ENABLED ✅" if trader.auto_buy else "DISABLED ❌"
        await update.message.reply_text(f"Auto‑trade {state} for your wallet.")

    async def cmd_settings(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        if not trader:
            await update.message.reply_text("No wallet found. /create_wallet")
            return

        if context.args and len(context.args) == 2:
            key, val = context.args[0].lower(), context.args[1]
            try:
                if key == "buy_amount":
                    trader.auto_buy_amount_sol = float(val)
                elif key == "slippage":
                    trader.slippage_bps = int(val)
                elif key == "stop_loss":
                    trader.stop_loss_pct = float(val)
                elif key == "max_positions":
                    trader.max_positions = int(val)
                else:
                    await update.message.reply_text(
                        "Unknown setting. Keys: buy_amount, slippage, stop_loss, max_positions"
                    )
                    return
                await update.message.reply_text(f"✅ Updated {key} = {val}")
            except ValueError:
                await update.message.reply_text("Invalid value.")
            return

        msg = (
            f"⚙️ <b>Your Trading Settings</b>\n\n"
            f"Buy amount:     <b>{trader.auto_buy_amount_sol} SOL</b>\n"
            f"Slippage:       <b>{trader.slippage_bps} bps</b>\n"
            f"Stop Loss:      <b>{trader.stop_loss_pct}%</b>\n"
            f"Max positions:  <b>{trader.max_positions}</b>\n"
            f"Auto‑trade:     <b>{'ON ✅' if trader.auto_buy else 'OFF ❌'}</b>\n\n"
            f"To edit: /settings &lt;key&gt; &lt;value&gt;\n"
            f"Keys: buy_amount | slippage | stop_loss | max_positions"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    # ──────────────────────────────────────────────────────────────────────────
    # Positions & PnL
    # ──────────────────────────────────────────────────────────────────────────

    async def cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        if not trader or not trader.positions:
            await update.message.reply_text("📊 No open positions.")
            return
        lines = ["📊 <b>Your Open Positions</b>\n"]
        for mint, p in trader.positions.items():
            change = 0.0
            if p.entry_price_sol > 0:
                change = (p.current_price_sol - p.entry_price_sol) / p.entry_price_sol * 100
            age_min = int((time.time() - p.buy_time) / 60)
            lines.append(
                f"• <b>{p.symbol}</b> <code>{mint[:8]}…</code>\n"
                f"  Entry: {p.entry_price_sol:.4f} | Now: {p.current_price_sol:.4f} | "
                f"{'📈' if change >= 0 else '📉'} {change:+.1f}% | {age_min}m ago"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        if not trader:
            await update.message.reply_text("No wallet found. /create_wallet")
            return
        unrealized = sum(
            (p.current_price_sol - p.entry_price_sol) * p.amount_sol / p.entry_price_sol
            for p in trader.positions.values()
            if p.entry_price_sol > 0
        )
        msg = (
            f"📈 <b>Profit / Loss Summary</b>\n\n"
            f"Realized PnL:   <b>{trader.realized_pnl_sol:+.4f} SOL</b>\n"
            f"Unrealized PnL: <b>{unrealized:+.4f} SOL</b>\n"
            f"Open positions: {len(trader.positions)}"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    # ──────────────────────────────────────────────────────────────────────────
    # Manual trade
    # ──────────────────────────────────────────────────────────────────────────

    async def cmd_buy(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        if not trader:
            await update.message.reply_text("Create a wallet first: /create_wallet")
            return
        if not context.args:
            await update.message.reply_text(
                "Usage: /buy &lt;mint_address&gt;", parse_mode=ParseMode.HTML
            )
            return
        mint = context.args[0]
        sym = mint[:6].upper()
        await update.message.reply_text(f"⏳ Placing buy order for {sym}…")
        tid = await trader.execute_buy(mint, sym)
        if tid:
            await update.message.reply_text(
                f"✅ Buy order placed!\nToken: <code>{mint}</code>", parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text(
                "❌ Buy failed. Check your balance, position limit, or token mcap."
            )

    async def cmd_sell(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        if not trader:
            await update.message.reply_text("Create a wallet first: /create_wallet")
            return
        if not context.args:
            await update.message.reply_text(
                "Usage: /sell &lt;mint_address&gt;", parse_mode=ParseMode.HTML
            )
            return
        mint = context.args[0]
        await update.message.reply_text("⏳ Selling position…")
        res = await trader.execute_sell(mint, 1.0)
        if res:
            await update.message.reply_text(
                f"✅ Position closed: <code>{mint}</code>", parse_mode=ParseMode.HTML
            )
        else:
            await update.message.reply_text(
                "❌ Sell failed — no open position found for that mint."
            )

    # ──────────────────────────────────────────────────────────────────────────
    # Spikes
    # ──────────────────────────────────────────────────────────────────────────

    async def cmd_spikes(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            import web_dashboard.app as dash
            det = dash.detector
        except Exception:
            det = None
        if not det:
            await update.message.reply_text("⏳ Detector not ready yet. Try again in a moment.")
            return
        spiked = det.get_spiked_tokens()
        if not spiked:
            await update.message.reply_text("😴 No tokens spiking ≥150% right now. Check back soon.")
            return
        lines = ["🔥 <b>Top Spikes ≥150%</b>\n"]
        for t in sorted(spiked, key=lambda x: x.spike_pct, reverse=True)[:8]:
            lines.append(
                f"• <b>{t.symbol}</b> +{t.spike_pct:.0f}% | "
                f"MCap {t.current_mcap:.2f} SOL | "
                f"<a href='https://pump.fun/coin/{t.mint}'>pump.fun</a>"
            )
        await update.message.reply_text(
            "\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Market Making – user flow
    # ──────────────────────────────────────────────────────────────────────────

    async def cmd_mm_purchase(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if len(context.args) < 3:
            strategies = "\n".join(
                f"  <b>{k}</b> – {v['label']} – {MM_PRICES_SOL[k]} SOL/hr\n    {v['description']}"
                for k, v in MM_STRATEGIES.items()
            )
            await update.message.reply_text(
                "Usage: /mm_purchase &lt;mint&gt; &lt;strategy&gt; &lt;hours&gt;\n\n"
                f"<b>Available strategies:</b>\n{strategies}",
                parse_mode=ParseMode.HTML,
            )
            return
        mint = context.args[0]
        strategy = context.args[1].lower()
        if strategy not in MM_STRATEGIES:
            await update.message.reply_text(
                f"❌ Unknown strategy. Choose: {', '.join(MM_STRATEGIES.keys())}"
            )
            return
        try:
            hours = int(context.args[2])
            assert 1 <= hours <= 720
        except Exception:
            await update.message.reply_text("Hours must be an integer between 1 and 720.")
            return
        uid = update.effective_user.id
        kp = self.user_wallets.get(uid)
        if not kp:
            await update.message.reply_text("Create a wallet first: /create_wallet")
            return
        if not PRIVATE_KEY:
            await update.message.reply_text("Admin payment wallet not configured. Contact support.")
            return
        total_cost = MM_PRICES_SOL[strategy] * hours
        s = MM_STRATEGIES[strategy]
        admin_kp = Keypair.from_base58_string(PRIVATE_KEY)
        await update.message.reply_text(
            f"🏦 <b>Market Making Order Summary</b>\n\n"
            f"Token mint:  <code>{mint}</code>\n"
            f"Strategy:    {s['label']}\n"
            f"  • Spread:  {s['spread_bps']} bps\n"
            f"  • Order size: {s['order_size_sol']} SOL\n"
            f"  • Max inventory: {s['max_inventory_sol']} SOL\n"
            f"Duration:    {hours} hour{'s' if hours > 1 else ''}\n"
            f"Price:       <b>{MM_PRICES_SOL[strategy]} SOL/hr</b>\n"
            f"Total cost:  <b>{total_cost:.2f} SOL</b>\n\n"
            f"📤 Send exactly <b>{total_cost:.4f} SOL</b> to:\n"
            f"<code>{admin_kp.pubkey()}</code>\n\n"
            f"After payment, run:\n/mm_confirm {mint}",
            parse_mode=ParseMode.HTML,
        )

    async def cmd_mm_confirm(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "Usage: /mm_confirm &lt;mint&gt;", parse_mode=ParseMode.HTML
            )
            return
        mint = context.args[0]
        user = update.effective_user
        existing = next(
            (r for r in self.mm_requests if r["user_id"] == user.id and r["mint"] == mint), None
        )
        strategy = existing["strategy"] if existing else "basic"
        hours = existing["hours"] if existing else 24
        req = {
            "user_id": user.id,
            "username": user.username or user.full_name,
            "mint": mint,
            "strategy": strategy,
            "hours": hours,
            "confirmed_at": time.strftime("%Y-%m-%d %H:%M UTC"),
        }
        self.mm_requests.append(req)
        await update.message.reply_text(
            "✅ <b>Payment confirmation received!</b>\n\n"
            f"Token: <code>{mint}</code>\n"
            f"Strategy: {strategy}\n"
            f"Duration: {hours}h\n\n"
            "Admin will verify payment and activate MM shortly.",
            parse_mode=ParseMode.HTML,
        )
        try:
            await self.bot.send_message(
                chat_id=int(self.admin_id),
                text=(
                    f"📩 <b>MM Payment Confirmation</b>\n\n"
                    f"User: @{user.username or user.full_name} (<code>{user.id}</code>)\n"
                    f"Mint: <code>{mint}</code>\n"
                    f"Strategy: {strategy}\n"
                    f"Duration: {hours}h\n"
                    f"Expected: {MM_PRICES_SOL.get(strategy, 0) * hours:.4f} SOL\n\n"
                    f"Run: /admin_mm_start {mint} {strategy}"
                ),
                parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            logger.error(f"Admin MM notify failed: {e}")

    async def cmd_mm_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        sessions = [s for s in self.mm_sessions.values() if s.get("user_id") == uid]
        if not sessions:
            await update.message.reply_text("You have no active MM sessions.")
            return
        lines = ["🏦 <b>Your Active MM Sessions</b>\n"]
        for s in sessions:
            elapsed = int((time.time() - s["started_at"]) / 3600)
            remaining = max(0, s["hours"] - elapsed)
            lines.append(
                f"• <code>{s['mint'][:10]}…</code> | {s['strategy']} | {remaining}h remaining"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # ──────────────────────────────────────────────────────────────────────────
    # Admin commands
    # ──────────────────────────────────────────────────────────────────────────

    async def cmd_admin_mm_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._is_admin(update):
            await update.message.reply_text("⛔ Admin only.")
            return
        if len(context.args) < 2:
            await update.message.reply_text(
                "Usage: /admin_mm_start &lt;mint&gt; &lt;strategy&gt;\n"
                "Strategies: basic | aggressive | deep",
                parse_mode=ParseMode.HTML,
            )
            return
        mint = context.args[0]
        strategy = context.args[1].lower()
        if strategy not in MM_STRATEGIES:
            await update.message.reply_text(
                f"Unknown strategy. Choose: {', '.join(MM_STRATEGIES.keys())}"
            )
            return
        req = next((r for r in self.mm_requests if r["mint"] == mint), None)
        hours = req["hours"] if req else 24
        user_id = req["user_id"] if req else None
        s_cfg = MM_STRATEGIES[strategy]
        if self.mm:
            await self.mm.add_token(mint, strategy=strategy, config=s_cfg)
        self.mm_sessions[mint] = {
            "mint": mint,
            "strategy": strategy,
            "hours": hours,
            "started_at": time.time(),
            "user_id": user_id,
        }
        await update.message.reply_text(
            f"✅ <b>MM Started</b>\n\n"
            f"Token: <code>{mint}</code>\n"
            f"Strategy: {MM_STRATEGIES[strategy]['label']}\n"
            f"Duration: {hours}h",
            parse_mode=ParseMode.HTML,
        )
        if user_id and self.bot:
            try:
                await self.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🚀 <b>Market Making Activated!</b>\n\n"
                        f"Token: <code>{mint}</code>\n"
                        f"Strategy: {MM_STRATEGIES[strategy]['label']}\n"
                        f"Duration: {hours}h\n\nYour MM session is now live."
                    ),
                    parse_mode=ParseMode.HTML,
                )
            except TelegramError:
                pass
        self.mm_requests = [r for r in self.mm_requests if r["mint"] != mint]

    async def cmd_mm_requests(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._is_admin(update):
            await update.message.reply_text("⛔ Admin only.")
            return
        if not self.mm_requests:
            await update.message.reply_text("✅ No pending MM requests.")
            return
        lines = ["📩 <b>Pending MM Requests</b>\n"]
        for r in self.mm_requests[-15:]:
            lines.append(
                f"• @{r['username']} | <code>{r['mint'][:10]}…</code> | "
                f"{r['strategy']} | {r['hours']}h | {r.get('confirmed_at', '?')}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_users(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._is_admin(update):
            await update.message.reply_text("⛔ Admin only.")
            return
        users = self._db.get("users", {})
        if not users:
            await update.message.reply_text("No users registered yet.")
            return
        lines = [f"👥 <b>Registered Users ({len(users)})</b>\n"]
        for uid, u in list(users.items())[-20:]:
            lines.append(
                f"• {u.get('full_name', '?')} | @{u.get('username', 'N/A')} | "
                f"ID: <code>{uid}</code> | 🌍 {u.get('country', '?')} | {u.get('joined_at', '?')}"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_emergency_kill(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not await self._is_admin(update):
            await update.message.reply_text("⛔ Admin only.")
            return
        killed_positions = 0
        for trader in self.user_traders.values():
            for mint in list(trader.positions.keys()):
                await trader.execute_sell(mint, 1.0)
                killed_positions += 1
        if self.mm:
            await self.mm.emergency_kill()
        self.mm_sessions.clear()
        await update.message.reply_text(
            f"🛑 <b>Emergency Kill Executed</b>\n\n"
            f"Positions closed: {killed_positions}\n"
            f"MM sessions stopped: all",
            parse_mode=ParseMode.HTML,
        )

    # ──────────────────────────────────────────────────────────────────────────
    # Handler registration
    # ──────────────────────────────────────────────────────────────────────────

    def register_handlers(self, application: Application):
        a = application
        a.add_handler(CommandHandler("start", self.cmd_start))
        a.add_handler(CommandHandler("create_wallet", self.cmd_create_wallet))
        a.add_handler(CommandHandler("import_wallet", self.cmd_import_wallet))
        a.add_handler(CommandHandler("balance", self.cmd_balance))
        a.add_handler(CommandHandler("deposit", self.cmd_deposit))
        a.add_handler(CommandHandler("withdraw", self.cmd_withdraw))
        a.add_handler(CommandHandler("autotrade", self.cmd_autotrade))
        a.add_handler(CommandHandler("settings", self.cmd_settings))
        a.add_handler(CommandHandler("positions", self.cmd_positions))
        a.add_handler(CommandHandler("pnl", self.cmd_pnl))
        a.add_handler(CommandHandler("buy", self.cmd_buy))
        a.add_handler(CommandHandler("sell", self.cmd_sell))
        a.add_handler(CommandHandler("spikes", self.cmd_spikes))
        a.add_handler(CommandHandler("mm_purchase", self.cmd_mm_purchase))
        a.add_handler(CommandHandler("mm_confirm", self.cmd_mm_confirm))
        a.add_handler(CommandHandler("mm_status", self.cmd_mm_status))
        a.add_handler(CommandHandler("admin_mm_start", self.cmd_admin_mm_start))
        a.add_handler(CommandHandler("mm_requests", self.cmd_mm_requests))
        a.add_handler(CommandHandler("users", self.cmd_users))
        a.add_handler(CommandHandler("emergency_kill", self.cmd_emergency_kill))
        logger.info("All handlers registered.")
