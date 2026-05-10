import asyncio
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackContext
from telegram.error import TelegramError
from solders.keypair import Keypair
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts
from solana.transaction import Transaction
from solders.system_program import TransferParams, transfer
from solders.pubkey import Pubkey
from config import (TELEGRAM_BOT_TOKEN, TELEGRAM_SIGNAL_CHANNEL,
                    TELEGRAM_ADMIN_ID, SOLANA_RPC_URL, PRIVATE_KEY, DRY_RUN)
from utils import logger

class SignalBot:
    def __init__(self, trader=None, mm=None):
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN) if TELEGRAM_BOT_TOKEN else None
        self.trader = trader
        self.mm = mm
        self.auto_buy_enabled = True
        # Wallet set up for admin commands
        self.keypair = None
        if PRIVATE_KEY:
            try:
                self.keypair = Keypair.from_base58_string(PRIVATE_KEY)
            except Exception as e:
                logger.error(f"Invalid PRIVATE_KEY – wallet commands disabled: {e}")
        self.rpc_client = AsyncClient(SOLANA_RPC_URL) if SOLANA_RPC_URL else None

    # ── Existing spike / signal methods (unchanged) ──
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
            await self.bot.send_message(chat_id=TELEGRAM_SIGNAL_CHANNEL, text=text,
                                        parse_mode=ParseMode.HTML, reply_markup=keyboard)
        except TelegramError as e:
            logger.error(f"Failed to send spike: {e}")

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
            await self.bot.send_message(chat_id=TELEGRAM_SIGNAL_CHANNEL, text=text, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            logger.error(f"Failed to send signal: {e}")

    async def send_admin_log(self, message: str):
        if not self.bot:
            return
        try:
            await self.bot.send_message(chat_id=TELEGRAM_ADMIN_ID, text=message, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            logger.error(f"Failed to send admin log: {e}")

    # ── Existing admin‑only commands (unchanged) ──
    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Welcome message – shows different list for admin vs public."""
        user_id = str(update.effective_user.id)
        if user_id == TELEGRAM_ADMIN_ID:
            msg = ("Admin commands available.\n"
                   "Public commands: /spikes, /mm_status, /trades, /help\n"
                   "Admin commands: /balance, /send, /withdraw_all, /mm_start, /mm_stop, /emergency_kill, /toggle_auto_buy")
        else:
            msg = ("Welcome to NoBrainTrade! 🧠\n\n"
                   "Available commands:\n"
                   "/spikes – See tokens spiking +150%\n"
                   "/mm_status – Market maker overview\n"
                   "/trades – View open positions\n"
                   "/help – All commands")
        await update.message.reply_text(msg)

    async def mm_start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_user.id) != TELEGRAM_ADMIN_ID or not self.mm:
            return
        token = context.args[0] if context.args else None
        if not token:
            await update.message.reply_text("Usage: /mm_start <mint_address>")
            return
        await self.mm.add_token(token)
        await update.message.reply_text(f"Started MM on {token}")

    async def mm_stop_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_user.id) != TELEGRAM_ADMIN_ID or not self.mm:
            return
        token = context.args[0] if context.args else None
        if not token:
            await update.message.reply_text("Usage: /mm_stop <mint_address>")
            return
        await self.mm.remove_token(token)
        await update.message.reply_text(f"Stopped MM on {token}")

    async def mm_status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Shows MM status – public, no admin check needed."""
        if not self.mm:
            await update.message.reply_text("Market maker is not running.")
            return
        status = self.mm.get_status()
        await update.message.reply_text(status)

    async def emergency_kill_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_user.id) != TELEGRAM_ADMIN_ID:
            return
        if self.trader:
            await self.trader.emergency_kill()
        if self.mm:
            await self.mm.emergency_kill()
        await update.message.reply_text("🔴 Emergency kill executed.")

    async def toggle_auto_buy_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_user.id) != TELEGRAM_ADMIN_ID:
            return
        self.auto_buy_enabled = not self.auto_buy_enabled
        await update.message.reply_text(f"Auto-buy {'ENABLED' if self.auto_buy_enabled else 'DISABLED'}.")

    # ── Public commands ────────────────────────────
    async def spikes_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show current spiking tokens to anyone."""
        # We need access to the detector instance – we'll assume it's stored on the app or passed later.
        # For now, we'll use a placeholder that accesses the detector through the globals in app.py
        import web_dashboard.app as dash
        detector = dash.detector
        if not detector:
            await update.message.reply_text("Spike detector not yet initialized.")
            return
        spiked = detector.get_spiked_tokens()
        if not spiked:
            await update.message.reply_text("No tokens above 150% right now.")
            return
        # Show top 5
        lines = ["🔥 <b>Top Spiking Tokens (≥150%)</b>\n"]
        for t in sorted(spiked, key=lambda x: x.spike_pct, reverse=True)[:5]:
            lines.append(f"• <b>{t.symbol}</b> {t.name}: +{t.spike_pct:.0f}% | MCap {t.current_mcap:.2f} SOL")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def trades_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show current open positions (public, anonymised)."""
        if not self.trader:
            await update.message.reply_text("Trader not running.")
            return
        positions = self.trader.positions
        if not positions:
            await update.message.reply_text("No open positions.")
            return
        lines = ["📊 <b>Open Positions</b>\n"]
        for mint, pos in positions.items():
            lines.append(f"• {pos.symbol} ({mint[:6]}…) – Entry: {pos.entry_price_sol:.4f} SOL, Current: {pos.current_price_sol:.4f} SOL")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def help_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Show all available commands."""
        user_id = str(update.effective_user.id)
        is_admin = (user_id == TELEGRAM_ADMIN_ID)
        msg = "🧠 <b>NoBrainTrade Commands</b>\n\n"
        msg += "<b>Everyone:</b>\n"
        msg += "/spikes – Top spiking tokens\n"
        msg += "/mm_status – Market maker status\n"
        msg += "/trades – Open positions\n"
        msg += "/help – This help\n"
        if is_admin:
            msg += "\n<b>Admin only:</b>\n"
            msg += "/balance – SOL balance\n"
            msg += "/send &lt;addr&gt; &lt;amount&gt; – Send SOL\n"
            msg += "/withdraw_all &lt;addr&gt; – Withdraw all SOL\n"
            msg += "/mm_start &lt;mint&gt; – Start market making\n"
            msg += "/mm_stop &lt;mint&gt; – Stop market making\n"
            msg += "/emergency_kill – Sell all positions\n"
            msg += "/toggle_auto_buy – Enable/disable auto‑buy"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    # ── Wallet admin commands (unchanged) ──
    async def balance_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_user.id) != TELEGRAM_ADMIN_ID:
            return
        if not self.keypair or not self.rpc_client:
            await update.message.reply_text("Wallet not configured.")
            return
        try:
            pubkey = self.keypair.pubkey()
            resp = await self.rpc_client.get_balance(pubkey, commitment=Confirmed)
            balance_lamports = resp['result']['value']
            balance_sol = balance_lamports / 1e9
            await update.message.reply_text(f"💰 Balance: {balance_sol:.4f} SOL")
        except Exception as e:
            await update.message.reply_text(f"Error getting balance: {e}")

    async def send_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_user.id) != TELEGRAM_ADMIN_ID:
            return
        # ... (same as before, keep as you already have)
        if not self.keypair or not self.rpc_client:
            await update.message.reply_text("Wallet not configured.")
            return
        if len(context.args) != 2:
            await update.message.reply_text("Usage: /send <address> <amount>")
            return
        to_address = context.args[0]
        try:
            amount_sol = float(context.args[1])
        except ValueError:
            await update.message.reply_text("Invalid amount.")
            return
        if amount_sol <= 0:
            await update.message.reply_text("Amount must be positive.")
            return
        if DRY_RUN:
            await update.message.reply_text(f"DRY RUN: Would send {amount_sol} SOL to {to_address}")
            return
        try:
            to_pubkey = Pubkey.from_string(to_address)
            blockhash_resp = await self.rpc_client.get_latest_blockhash(commitment=Confirmed)
            blockhash = blockhash_resp['result']['value']['blockhash']
            ix = transfer(
                TransferParams(
                    from_pubkey=self.keypair.pubkey(),
                    to_pubkey=to_pubkey,
                    lamports=int(amount_sol * 1e9)
                )
            )
            tx = Transaction().add(ix)
            tx.recent_blockhash = blockhash
            tx.sign(self.keypair)
            tx_opts = TxOpts(skip_preflight=False, preflight_commitment=Confirmed)
            result = await self.rpc_client.send_transaction(tx, opts=tx_opts)
            txid = result['result']
            await update.message.reply_text(f"✅ Sent {amount_sol} SOL\nTX: {txid}")
        except Exception as e:
            await update.message.reply_text(f"Send failed: {e}")

    async def withdraw_all_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_user.id) != TELEGRAM_ADMIN_ID:
            return
        if not self.keypair or not self.rpc_client:
            await update.message.reply_text("Wallet not configured.")
            return
        if len(context.args) != 1:
            await update.message.reply_text("Usage: /withdraw_all <address>")
            return
        to_address = context.args[0]
        try:
            to_pubkey = Pubkey.from_string(to_address)
            pubkey = self.keypair.pubkey()
            balance_resp = await self.rpc_client.get_balance(pubkey, commitment=Confirmed)
            balance_lamports = balance_resp['result']['value']
            if balance_lamports <= 0:
                await update.message.reply_text("No SOL to withdraw.")
                return
            if DRY_RUN:
                await update.message.reply_text(f"DRY RUN: Would withdraw {balance_lamports/1e9:.4f} SOL to {to_address}")
                return
            fee = 5000
            amount_lamports = balance_lamports - fee
            if amount_lamports <= 0:
                await update.message.reply_text("Balance too low to cover fee.")
                return
            blockhash_resp = await self.rpc_client.get_latest_blockhash(commitment=Confirmed)
            blockhash = blockhash_resp['result']['value']['blockhash']
            ix = transfer(
                TransferParams(
                    from_pubkey=pubkey,
                    to_pubkey=to_pubkey,
                    lamports=amount_lamports
                )
            )
            tx = Transaction().add(ix)
            tx.recent_blockhash = blockhash
            tx.sign(self.keypair)
            result = await self.rpc_client.send_transaction(tx, opts=TxOpts(skip_preflight=False, preflight_commitment=Confirmed))
            txid = result['result']
            await update.message.reply_text(f"✅ Withdrawn {amount_lamports/1e9:.4f} SOL\nTX: {txid}")
        except Exception as e:
            await update.message.reply_text(f"Withdraw failed: {e}")

    # ── Register all handlers ──────────────────────
    def register_handlers(self, application: Application):
        # Public commands
        application.add_handler(CommandHandler("start", self.start_cmd))
        application.add_handler(CommandHandler("spikes", self.spikes_cmd))
        application.add_handler(CommandHandler("mm_status", self.mm_status_cmd))
        application.add_handler(CommandHandler("trades", self.trades_cmd))
        application.add_handler(CommandHandler("help", self.help_cmd))

        # Admin commands (still check user ID inside functions)
        application.add_handler(CommandHandler("balance", self.balance_cmd))
        application.add_handler(CommandHandler("send", self.send_cmd))
        application.add_handler(CommandHandler("withdraw_all", self.withdraw_all_cmd))
        application.add_handler(CommandHandler("mm_start", self.mm_start_cmd))
        application.add_handler(CommandHandler("mm_stop", self.mm_stop_cmd))
        application.add_handler(CommandHandler("emergency_kill", self.emergency_kill_cmd))
        application.add_handler(CommandHandler("toggle_auto_buy", self.toggle_auto_buy_cmd))
