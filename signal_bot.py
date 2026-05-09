import asyncio
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackContext
import logging
from config import (TELEGRAM_BOT_TOKEN, TELEGRAM_SIGNAL_CHANNEL,
                    TELEGRAM_ADMIN_ID, DRY_RUN)
from utils import logger

class SignalBot:
    def __init__(self, trader=None, mm=None):
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN)
        self.trader = trader
        self.mm = mm
        self.auto_buy_enabled = True

    async def send_spike(self, token):
        """Send spike alert to channel."""
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
        await self._send_to_channel(text, reply_markup=keyboard)

    async def send_strong_signal(self, token):
        """Send strong buy signal (No Brain Score ≥85)."""
        text = (
            f"🧠 <b>BUY SIGNAL (Score ≥85)</b>\n\n"
            f"🪙 <b>{token.symbol} ({token.name})</b>\n"
            f"📈 Spike: +{token.spike_pct:.0f}%\n"
            f"💰 MCap: {token.current_mcap:.2f} SOL\n"
            f"📊 Buy Ratio: {token.buy_ratio:.2f}\n"
            f"<a href='https://pump.fun/coin/{token.mint}'>Open</a>"
        )
        await self._send_to_channel(text)

    async def send_admin_log(self, message: str):
        """Send trade/status log to admin DM."""
        try:
            await self.bot.send_message(chat_id=TELEGRAM_ADMIN_ID, text=message, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            logger.error(f"Failed to send admin log: {e}")

    async def _send_to_channel(self, text, reply_markup=None):
        try:
            await self.bot.send_message(chat_id=TELEGRAM_SIGNAL_CHANNEL,
                                        text=text, parse_mode=ParseMode.HTML,
                                        reply_markup=reply_markup,
                                        disable_web_page_preview=False)
        except TelegramError as e:
            logger.error(f"Channel message failed: {e}")

    # ── Telegram Bot Command Handlers ──────────────
    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_user.id) != TELEGRAM_ADMIN_ID:
            return
        await update.message.reply_text("NoBrainTrade bot active.\nCommands: /mm_start, /mm_stop, /mm_status, /emergency_kill, /toggle_auto_buy")

    async def mm_start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_user.id) != TELEGRAM_ADMIN_ID or not self.mm:
            return
        token = context.args[0] if context.args else None
        if not token:
            await update.message.reply_text("Usage: /mm_start <mint_address>")
            return
        await self.mm.add_token(token)
        await update.message.reply_text(f"Started market making on {token}")

    async def mm_stop_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_user.id) != TELEGRAM_ADMIN_ID or not self.mm:
            return
        token = context.args[0] if context.args else None
        if not token:
            await update.message.reply_text("Usage: /mm_stop <mint_address>")
            return
        await self.mm.remove_token(token)
        await update.message.reply_text(f"Stopped market making on {token}")

    async def mm_status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_user.id) != TELEGRAM_ADMIN_ID or not self.mm:
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
        await update.message.reply_text("🔴 Emergency kill executed. All positions sold, MM stopped.")

    async def toggle_auto_buy_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if str(update.effective_user.id) != TELEGRAM_ADMIN_ID:
            return
        self.auto_buy_enabled = not self.auto_buy_enabled
        await update.message.reply_text(f"Auto-buy {'ENABLED' if self.auto_buy_enabled else 'DISABLED'}.")

    def register_handlers(self, application: Application):
        application.add_handler(CommandHandler("start", self.start_cmd))
        application.add_handler(CommandHandler("mm_start", self.mm_start_cmd))
        application.add_handler(CommandHandler("mm_stop", self.mm_stop_cmd))
        application.add_handler(CommandHandler("mm_status", self.mm_status_cmd))
        application.add_handler(CommandHandler("emergency_kill", self.emergency_kill_cmd))
        application.add_handler(CommandHandler("toggle_auto_buy", self.toggle_auto_buy_cmd))
