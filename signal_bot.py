import asyncio, time, uuid
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import TelegramError
from solders.keypair import Keypair
from solders.transaction import Transaction
from solders.system_program import transfer, TransferParams
from solders.pubkey import Pubkey
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
import aiohttp
from dataclasses import dataclass, field
from typing import Dict, Optional, List

from config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_SIGNAL_CHANNEL, TELEGRAM_ADMIN_ID,
    SOLANA_RPC_URL, DRY_RUN, SLIPPAGE_BPS, STOP_LOSS_PCT
)
from utils import logger

# ----------------------------------------------------------------------
#  Data models for per‑user trading
# ----------------------------------------------------------------------
@dataclass
class Position:
    mint: str
    symbol: str
    entry_price_sol: float
    amount_sol: float
    current_price_sol: float = 0.0
    highest_price_sol: float = 0.0
    buy_time: float = field(default_factory=time.time)
    tp_levels: list = field(default_factory=lambda: [(2.0,0.5),(3.0,0.3),(5.0,0.2)])
    id: str = field(default_factory=lambda: uuid.uuid4().hex)

class UserTrader:
    """Handles trades for a single user with their own keypair."""
    def __init__(self, user_id: int, keypair: Keypair):
        self.user_id = user_id
        self.keypair = keypair
        self.positions: Dict[str, Position] = {}
        self.auto_buy = False
        self.auto_buy_amount_sol = 0.05
        self.max_positions = 3
        self.slippage_bps = SLIPPAGE_BPS
        self.stop_loss_pct = STOP_LOSS_PCT
        self.take_profit_levels = [(2.0,0.5),(3.0,0.3),(5.0,0.2)]
        self.session = aiohttp.ClientSession()

    async def get_token_price(self, mint: str) -> Optional[float]:
        try:
            async with self.session.get(f"https://frontend-api.pump.fun/coins/{mint}") as resp:
                data = await resp.json()
                return float(data.get("market_cap", 0))
        except:
            return None

    async def execute_buy(self, mint: str, symbol: str) -> Optional[str]:
        if len(self.positions) >= self.max_positions:
            return None
        price = await self.get_token_price(mint)
        if not price or price > 500:  # mcap filter
            return None
        amount = self.auto_buy_amount_sol
        if DRY_RUN:
            pos = Position(mint, symbol, price or 0, amount)
            self.positions[mint] = pos
            return pos.id
        payload = {
            "action": "buy", "mint": mint, "amount": amount,
            "denominatedInSol": "true", "slippage": self.slippage_bps,
            "priorityFee": 0.005, "privateKey": str(self.keypair)
        }
        async with self.session.post("https://pumpportal.fun/api/trade", json=payload) as resp:
            data = await resp.json()
            if data.get("error"):
                logger.error(f"User {self.user_id} buy error: {data}")
                return None
        pos = Position(mint, symbol, price, amount)
        self.positions[mint] = pos
        logger.info(f"User {self.user_id} bought {amount} SOL of {symbol}")
        return pos.id

    async def execute_sell(self, mint: str, fraction: float = 1.0) -> bool:
        if mint not in self.positions:
            return False
        pos = self.positions[mint]
        amount = pos.amount_sol * fraction
        if DRY_RUN:
            logger.info(f"User {self.user_id} DRY SELL {amount} SOL of {pos.symbol}")
            pos.amount_sol -= amount
            if pos.amount_sol <= 0.0001:
                del self.positions[mint]
            return True
        payload = {
            "action": "sell", "mint": mint, "amount": amount,
            "denominatedInSol": "true", "slippage": self.slippage_bps,
            "privateKey": str(self.keypair)
        }
        async with self.session.post("https://pumpportal.fun/api/trade", json=payload) as resp:
            data = await resp.json()
            if data.get("error"):
                logger.error(f"User {self.user_id} sell error: {data}")
                return False
        pos.amount_sol -= amount
        if pos.amount_sol <= 0.0001:
            del self.positions[mint]
        logger.info(f"User {self.user_id} sold {amount} SOL of {pos.symbol}")
        return True

    async def monitor_positions(self):
        """Continuously check TP/SL for this user."""
        while True:
            for mint, pos in list(self.positions.items()):
                price = await self.get_token_price(mint)
                if not price:
                    continue
                pos.current_price_sol = price
                if price > pos.highest_price_sol:
                    pos.highest_price_sol = price
                # Stop loss
                if pos.highest_price_sol > 0:
                    drawdown = (pos.highest_price_sol - price) / pos.highest_price_sol * 100
                    if drawdown >= self.stop_loss_pct:
                        await self.execute_sell(mint, 1.0)
                        continue
                # Take profit
                for mult, frac in list(pos.tp_levels):
                    if price >= pos.entry_price_sol * mult:
                        await self.execute_sell(mint, frac)
                        pos.tp_levels.remove((mult, frac))
                        break
            await asyncio.sleep(5)

# ----------------------------------------------------------------------
#  Main SignalBot
# ----------------------------------------------------------------------
class SignalBot:
    def __init__(self, trader=None, mm=None):
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None
        self.admin_id = TELEGRAM_ADMIN_ID
        self.rpc_client = AsyncClient(SOLANA_RPC_URL) if SOLANA_RPC_URL else None
        # User‑wallet storage (in‑memory – replace with DB for production!)
        self.user_wallets: Dict[int, Keypair] = {}
        self.user_traders: Dict[int, UserTrader] = {}
        # MM purchase requests
        self.mm_requests = []

    async def _is_admin(self, update: Update) -> bool:
        return str(update.effective_user.id) == self.admin_id

    # ── Spike / Signal sending (to channel, unchanged) ─────
    async def send_spike(self, token): ...
    async def send_strong_signal(self, token): ...
    async def send_admin_log(self, msg): ...

    # ──────────────────────────────────────────────────────
    #  WALLET COMMANDS (everyone)
    # ──────────────────────────────────────────────────────
    async def cmd_create_wallet(self, update, context):
        uid = update.effective_user.id
        if uid in self.user_wallets:
            await update.message.reply_text("You already have a wallet.")
            return
        keypair = Keypair()
        self.user_wallets[uid] = keypair
        self.user_traders[uid] = UserTrader(uid, keypair)
        addr = str(keypair.pubkey())
        priv = str(keypair)  # base58 private key
        await update.message.reply_text(
            f"✅ <b>Wallet Created!</b>\n\n"
            f"📤 Address:\n<code>{addr}</code>\n\n"
            f"🔐 Private key (save it!):\n<code>{priv}</code>\n\n"
            f"Deposit SOL to your address to start trading.",
            parse_mode=ParseMode.HTML
        )

    async def cmd_balance(self, update, context):
        uid = update.effective_user.id
        kp = self.user_wallets.get(uid)
        if not kp:
            await update.message.reply_text("Create a wallet first: /create_wallet")
            return
        try:
            resp = await self.rpc_client.get_balance(kp.pubkey(), commitment=Confirmed)
            bal = resp['result']['value'] / 1e9
            await update.message.reply_text(f"💰 Balance: {bal:.4f} SOL")
        except Exception as e:
            await update.message.reply_text(f"Error: {e}")

    async def cmd_deposit(self, update, context):
        uid = update.effective_user.id
        kp = self.user_wallets.get(uid)
        if not kp:
            await update.message.reply_text("Create a wallet first: /create_wallet")
            return
        await update.message.reply_text(
            f"Your deposit address:\n<code>{kp.pubkey()}</code>",
            parse_mode=ParseMode.HTML
        )

    async def cmd_withdraw(self, update, context):
        uid = update.effective_user.id
        kp = self.user_wallets.get(uid)
        if not kp or not self.rpc_client:
            await update.message.reply_text("No wallet or RPC down.")
            return
        if len(context.args) != 2:
            await update.message.reply_text("Usage: /withdraw <address> <amount>")
            return
        to_addr, amt_str = context.args[0], context.args[1]
        try:
            amount = float(amt_str)
        except:
            await update.message.reply_text("Invalid amount.")
            return
        if amount <= 0:
            return
        if DRY_RUN:
            await update.message.reply_text(f"DRY RUN: Would send {amount} SOL to {to_addr}")
            return
        try:
            to_pubkey = Pubkey.from_string(to_addr)
            blockhash = (await self.rpc_client.get_latest_blockhash(commitment=Confirmed))['result']['value']['blockhash']
            ix = transfer(TransferParams(from_pubkey=kp.pubkey(), to_pubkey=to_pubkey, lamports=int(amount*1e9)))
            tx = Transaction().add(ix)
            tx.recent_blockhash = blockhash
            tx.sign(kp)
            result = await self.rpc_client.send_transaction(tx, TxOpts(skip_preflight=False, preflight_commitment=Confirmed))
            await update.message.reply_text(f"✅ Sent {amount} SOL\nTX: <code>{result['result']}</code>", parse_mode=ParseMode.HTML)
        except Exception as e:
            await update.message.reply_text(f"Withdraw failed: {e}")

    # ── AUTO TRADE SETTINGS ─────────────────────
    async def cmd_autotrade(self, update, context):
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        if not trader:
            await update.message.reply_text("Create a wallet first: /create_wallet")
            return
        args = context.args
        if not args or args[0].lower() not in ("on", "off"):
            await update.message.reply_text("Usage: /autotrade on|off")
            return
        trader.auto_buy = (args[0].lower() == "on")
        await update.message.reply_text(f"Auto‑trade {'ENABLED' if trader.auto_buy else 'DISABLED'} for your wallet.")

    async def cmd_settings(self, update, context):
        """View / change trading parameters via inline buttons."""
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        if not trader:
            await update.message.reply_text("No wallet found. /create_wallet")
            return
        msg = (
            f"⚙️ <b>Your settings</b>\n"
            f"Buy amount: {trader.auto_buy_amount_sol} SOL\n"
            f"Slippage: {trader.slippage_bps} bps\n"
            f"Stop Loss: {trader.stop_loss_pct}%\n"
            f"Max positions: {trader.max_positions}\n"
            f"Auto‑trade: {'ON' if trader.auto_buy else 'OFF'}"
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("Set Buy Amt", callback_data="set_buy"),
             InlineKeyboardButton("Set Slippage", callback_data="set_slip")],
            [InlineKeyboardButton("Set Stop Loss", callback_data="set_sl"),
             InlineKeyboardButton("Set Max Posis", callback_data="set_maxpos")],
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)

    async def callback_handler(self, update, context):
        query = update.callback_query
        await query.answer()
        uid = query.from_user.id
        trader = self.user_traders.get(uid)
        if not trader:
            return
        data = query.data
        # ... implement step‑by‑step value input via conversation or inline prompt
        # For brevity, here we just acknowledge.
        await query.edit_message_text("Feature coming soon. Use /settings to see current values.")

    # ── POSITIONS & PNL ────────────────────────
    async def cmd_positions(self, update, context):
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        if not trader or not trader.positions:
            await update.message.reply_text("No open positions.")
            return
        lines = [f"📊 <b>Your open positions</b>\n"]
        for mint, p in trader.positions.items():
            lines.append(f"• {p.symbol} ({mint[:6]}…) – Entry: {p.entry_price_sol:.4f} SOL, Current: {p.current_price_sol:.4f} SOL, SL: {trader.stop_loss_pct}%")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def cmd_pnl(self, update, context):
        # Store closed PnL (we'll skip for now)
        await update.message.reply_text("PnL tracking coming soon.")

    # ── MANUAL BUY/SELL ─────────────────────────
    async def cmd_buy(self, update, context):
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        if not trader:
            await update.message.reply_text("Create a wallet first.")
            return
        if not context.args:
            await update.message.reply_text("Usage: /buy <mint>")
            return
        mint = context.args[0]
        sym = mint[:6]
        tid = await trader.execute_buy(mint, sym)
        await update.message.reply_text(f"Buy order sent." if tid else "Buy failed.")

    async def cmd_sell(self, update, context):
        uid = update.effective_user.id
        trader = self.user_traders.get(uid)
        if not trader:
            await update.message.reply_text("Create a wallet first.")
            return
        if not context.args:
            await update.message.reply_text("Usage: /sell <mint>")
            return
        mint = context.args[0]
        res = await trader.execute_sell(mint, 1.0)
        await update.message.reply_text("Sell successful." if res else "Sell failed (no position).")

    # ── MARKET MAKING PURCHASE (dev pays) ───────
    async def cmd_mm_purchase(self, update, context):
        """Dev pays SOL → admin approves MM."""
        if not context.args:
            await update.message.reply_text("Usage: /mm_purchase <mint_address> <amount_SOL_to_pay>")
            return
        mint, amt_str = context.args[0], context.args[1]
        try:
            amount = float(amt_str)
        except:
            await update.message.reply_text("Invalid amount.")
            return
        user = update.effective_user
        # User must have wallet
        kp = self.user_wallets.get(user.id)
        if not kp:
            await update.message.reply_text("Create a wallet first.")
            return
        # Simple flow: they send SOL to admin wallet and we notify
        admin_kp = Keypair.from_base58_string(PRIVATE_KEY) if PRIVATE_KEY else None
        if not admin_kp:
            await update.message.reply_text("Admin wallet not configured.")
            return
        await update.message.reply_text(
            f"Send exactly {amount} SOL to admin address:\n<code>{admin_kp.pubkey()}</code>\n"
            f"Then use /mm_confirm {mint} to notify admin.",
            parse_mode=ParseMode.HTML
        )

    async def cmd_mm_confirm(self, update, context):
        """User notifies admin they paid for MM."""
        if not context.args:
            await update.message.reply_text("Usage: /mm_confirm <mint>")
            return
        mint = context.args[0]
        user = update.effective_user
        self.mm_requests.append({"user_id": user.id, "username": user.username, "mint": mint})
        try:
            await self.bot.send_message(chat_id=self.admin_id,
                                        text=f"📩 MM purchase by {user.username} for {mint}. Check wallet.")
        except: pass
        await update.message.reply_text("Payment notification sent. Admin will activate MM soon.")

    # ── ADMIN MM control ─────────────────────────
    async def cmd_admin_mm_start(self, update, context):
        if not await self._is_admin(update): return
        # Start MM using central wallet (admin’s keypair)
        # This uses the existing MarketMaker class, but admin must already have it.
        from web_dashboard.app import market_maker as mm
        if not context.args:
            await update.message.reply_text("Usage: /admin_mm_start <mint>")
            return
        mint = context.args[0]
        await mm.add_token(mint)
        await update.message.reply_text(f"MM started for {mint} (admin wallet).")

    # ── PUBLIC UTILITIES ────────────────────────
    async def cmd_spikes(self, update, context):
        import web_dashboard.app as dash
        det = dash.detector
        if not det:
            await update.message.reply_text("Detector not ready.")
            return
        spiked = det.get_spiked_tokens()
        if not spiked:
            await update.message.reply_text("No spikes now.")
            return
        lines = ["🔥 <b>Top Spikes</b>\n"]
        for t in sorted(spiked, key=lambda x: x.spike_pct, reverse=True)[:5]:
            lines.append(f"• <b>{t.symbol}</b> +{t.spike_pct:.0f}% | MCap {t.current_mcap:.2f} SOL")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    # ── REGISTER HANDLERS ───────────────────────
    def register_handlers(self, app: Application):
        # Wallet
        app.add_handler(CommandHandler("create_wallet", self.cmd_create_wallet))
        app.add_handler(CommandHandler("balance", self.cmd_balance))
        app.add_handler(CommandHandler("deposit", self.cmd_deposit))
        app.add_handler(CommandHandler("withdraw", self.cmd_withdraw))
        # Auto trade
        app.add_handler(CommandHandler("autotrade", self.cmd_autotrade))
        app.add_handler(CommandHandler("settings", self.cmd_settings))
        app.add_handler(CallbackQueryHandler(self.callback_handler))
        # Positions
        app.add_handler(CommandHandler("positions", self.cmd_positions))
        app.add_handler(CommandHandler("pnl", self.cmd_pnl))
        app.add_handler(CommandHandler("buy", self.cmd_buy))
        app.add_handler(CommandHandler("sell", self.cmd_sell))
        # MM
        app.add_handler(CommandHandler("mm_purchase", self.cmd_mm_purchase))
        app.add_handler(CommandHandler("mm_confirm", self.cmd_mm_confirm))
        app.add_handler(CommandHandler("admin_mm_start", self.cmd_admin_mm_start))
        # Public
        app.add_handler(CommandHandler("spikes", self.cmd_spikes))
        # (you can keep start, help, etc. – we’ll skip for brevity)
