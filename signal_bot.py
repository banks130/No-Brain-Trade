import asyncio
import time
from datetime import datetime
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler
from config import (TELEGRAM_BOT_TOKEN, TELEGRAM_SIGNAL_CHANNEL,
                    TELEGRAM_ADMIN_ID, DRY_RUN, AUTO_BUY_AMOUNT_SOL,
                    STOP_LOSS_PCT, TAKE_PROFIT_LEVELS)
from utils import logger


def _now():
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

def _sol(val):
    return f"{val:.4f} SOL"

def _pct(val):
    sign = "+" if val >= 0 else ""
    return f"{sign}{val:.1f}%"


class SignalBot:
    def __init__(self, trader=None, mm=None):
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN)
        self.trader = trader
        self.mm = mm
        self.auto_buy_enabled = True
        self._user_count = 0
        self._wallet_count = 0

    # ── Core send helpers ──────────────────────────────────────────────────

    async def _send(self, chat_id, text, keyboard=None, preview=False):
        try:
            await self.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
                disable_web_page_preview=not preview,
            )
        except TelegramError as e:
            logger.error(f"Telegram send error: {e}")

    async def _channel(self, text, keyboard=None):
        await self._send(TELEGRAM_SIGNAL_CHANNEL, text, keyboard)

    async def _admin(self, text, keyboard=None):
        await self._send(TELEGRAM_ADMIN_ID, text, keyboard)

    # ── Token / Spike notifications ────────────────────────────────────────

    async def send_new_token(self, token):
        """Fires when a brand new token is first seen."""
        text = (
            f"🆕 <b>New Token Detected</b>\n\n"
            f"🪙 <b>${token.symbol}</b> — {token.name}\n"
            f"📍 <code>{token.mint}</code>\n"
            f"💰 Initial MCap: <b>{token.initial_mcap:.2f} SOL</b>\n"
            f"⏰ {_now()}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔍 pump.fun", url=f"https://pump.fun/coin/{token.mint}"),
            InlineKeyboardButton("📊 Dexscreener", url=f"https://dexscreener.com/solana/{token.mint}"),
        ]])
        await self._channel(text, kb)

    async def send_spike(self, token):
        """Fires when token hits ≥150% spike."""
        score = self._calc_score(token)
        bar = self._score_bar(score)
        spike_emoji = "💀" if token.spike_pct >= 500 else "🌙" if token.spike_pct >= 300 else "🚀"

        text = (
            f"{spike_emoji} <b>SPIKE ALERT ≥150%</b>\n\n"
            f"🪙 <b>${token.symbol}</b> — {token.name}\n"
            f"📍 <code>{token.mint}</code>\n\n"
            f"📈 Spike: <b>+{token.spike_pct:.0f}%</b>\n"
            f"💰 MCap Now: <b>{token.current_mcap:.2f} SOL</b>\n"
            f"🏔 Peak MCap: <b>{token.peak_mcap:.2f} SOL</b>\n"
            f"👥 Wallets: <b>{token.unique_wallet_count}</b>\n"
            f"📊 Buy Ratio: <b>{token.buy_ratio:.0%}</b>\n"
            f"💸 Net Flow: <b>{_sol(token.net_sol_flow)}</b>\n"
            f"⏱ Age: <b>{self._age(token.age_seconds)}</b>\n\n"
            f"🧠 No Brain Score: <b>{score}/100</b>\n"
            f"{bar}\n\n"
            f"{'🤖 <b>AUTO-BUY QUEUED</b>' if self.auto_buy_enabled else '⏸ Auto-buy OFF'}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🟢 Buy on pump.fun", url=f"https://pump.fun/coin/{token.mint}"),
            InlineKeyboardButton("📊 Chart", url=f"https://dexscreener.com/solana/{token.mint}"),
        ]])
        await self._channel(text, kb)

    async def send_strong_signal(self, token):
        """Fires when No Brain Score ≥85."""
        score = self._calc_score(token)
        text = (
            f"🧠 <b>STRONG BUY SIGNAL</b> — Score {score}/100\n\n"
            f"🪙 <b>${token.symbol}</b> — {token.name}\n"
            f"📍 <code>{token.mint}</code>\n\n"
            f"📈 Spike: <b>+{token.spike_pct:.0f}%</b>\n"
            f"💰 MCap: <b>{token.current_mcap:.2f} SOL</b>\n"
            f"📊 Buy Ratio: <b>{token.buy_ratio:.0%}</b>\n"
            f"👥 Wallets: <b>{token.unique_wallet_count}</b>\n"
            f"💸 Net Flow: <b>{_sol(token.net_sol_flow)}</b>"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("⚡ Buy Now", url=f"https://pump.fun/coin/{token.mint}"),
        ]])
        await self._channel(text, kb)
        await self._admin(f"🧠 Strong signal: <b>${token.symbol}</b> score {score}/100 — auto-buy {'queued' if self.auto_buy_enabled else 'OFF'}")

    # ── Trade notifications ────────────────────────────────────────────────

    async def notify_buy_executed(self, token_symbol, mint, amount_sol, mcap_sol, position_id, dry=False):
        tp_str = " | ".join([f"+{int((m-1)*100)}% → sell {int(f*100)}%" for m, f in TAKE_PROFIT_LEVELS]) if TAKE_PROFIT_LEVELS else "—"
        text = (
            f"{'🧪 DRY RUN — ' if dry else ''}🟢 <b>BUY EXECUTED</b>\n\n"
            f"🤖 Auto-buy\n"
            f"🪙 <b>${token_symbol}</b>\n"
            f"📍 <code>{mint}</code>\n"
            f"💰 <b>{_sol(amount_sol)}</b>\n"
            f"📊 Entry MCap: <b>{mcap_sol:.2f} SOL</b>\n\n"
            f"🎯 TP Levels: {tp_str}\n"
            f"🛑 Stop Loss: <b>-{STOP_LOSS_PCT}%</b>\n"
            f"🆔 Position: <code>{position_id}</code>\n"
            f"⏰ {_now()}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Chart", url=f"https://dexscreener.com/solana/{mint}"),
            InlineKeyboardButton("💊 pump.fun", url=f"https://pump.fun/coin/{mint}"),
        ]])
        await self._admin(text, kb)
        await self._channel(text, kb)

    async def notify_sell_executed(self, token_symbol, mint, amount_sol, reason, pnl_sol, pnl_pct, dry=False):
        pnl_emoji = "✅" if pnl_sol >= 0 else "❌"
        reason_map = {
            "tp": "🎯 Take Profit Hit",
            "sl": "🛑 Stop Loss Hit",
            "kill": "☠️ Emergency Kill",
            "manual": "👤 Manual Sell",
        }
        reason_label = reason_map.get(reason, reason)
        text = (
            f"{'🧪 DRY RUN — ' if dry else ''}🔴 <b>SELL EXECUTED</b>\n\n"
            f"{reason_label}\n"
            f"🪙 <b>${token_symbol}</b>\n"
            f"📍 <code>{mint}</code>\n"
            f"💰 Sold: <b>{_sol(amount_sol)}</b>\n\n"
            f"{pnl_emoji} PnL: <b>{_sol(pnl_sol)}</b> (<b>{_pct(pnl_pct)}</b>)\n"
            f"⏰ {_now()}"
        )
        await self._admin(text)
        await self._channel(text)

    async def notify_take_profit(self, token_symbol, mint, level_pct, fraction, pnl_sol):
        text = (
            f"🎯 <b>TAKE PROFIT HIT</b>\n\n"
            f"🪙 <b>${token_symbol}</b>\n"
            f"📈 TP Level: <b>+{level_pct:.0f}%</b>\n"
            f"📤 Sold: <b>{int(fraction*100)}%</b> of position\n"
            f"💵 Realized: <b>{_sol(pnl_sol)}</b>\n"
            f"⏰ {_now()}"
        )
        await self._admin(text)
        await self._channel(text)

    async def notify_stop_loss(self, token_symbol, mint, drawdown_pct, pnl_sol):
        text = (
            f"🛑 <b>STOP LOSS TRIGGERED</b>\n\n"
            f"🪙 <b>${token_symbol}</b>\n"
            f"📉 Drawdown: <b>{_pct(-drawdown_pct)}</b>\n"
            f"💸 Loss: <b>{_sol(pnl_sol)}</b>\n"
            f"⏰ {_now()}"
        )
        await self._admin(text)
        await self._channel(text)

    async def notify_kill_switch(self, positions_closed, total_pnl):
        text = (
            f"☠️ <b>EMERGENCY KILL SWITCH ACTIVATED</b>\n\n"
            f"📤 Positions closed: <b>{positions_closed}</b>\n"
            f"💰 Total PnL: <b>{_sol(total_pnl)}</b>\n"
            f"⏰ {_now()}"
        )
        await self._admin(text)
        await self._channel(text)

    async def notify_error(self, context: str, error: str):
        text = (
            f"⚠️ <b>ERROR</b>\n\n"
            f"📍 Context: {context}\n"
            f"❌ {error}\n"
            f"⏰ {_now()}"
        )
        await self._admin(text)

    async def send_admin_log(self, message: str):
        await self._admin(message)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _calc_score(self, token) -> int:
        if token.spike_pct < 150:
            return 0
        score = 0
        score += min((token.spike_pct - 150) * 0.5, 25)
        score += min(token.buy_ratio * 20, 20)
        score += min(token.unique_wallet_count / 5 * 15, 15)
        return min(int(score), 100)

    def _score_bar(self, score) -> str:
        filled = int(score / 10)
        return "█" * filled + "░" * (10 - filled) + f" {score}/100"

    def _age(self, seconds) -> str:
        if seconds < 60:
            return f"{int(seconds)}s"
        if seconds < 3600:
            return f"{int(seconds/60)}m"
        return f"{int(seconds/3600)}h"

    # ── Telegram Command Handlers ──────────────────────────────────────────

    def _is_admin(self, update: Update) -> bool:
        return str(update.effective_user.id) == str(TELEGRAM_ADMIN_ID)

    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        text = (
            f"🧠 <b>No Brain Trade — Admin Panel</b>\n\n"
            f"<b>Trading Commands:</b>\n"
            f"/autobuy — toggle auto-buy on/off\n"
            f"/buy &lt;mint&gt; — manual buy\n"
            f"/sell &lt;mint&gt; — manual sell\n"
            f"/positions — view open positions\n"
            f"/pnl — view PnL summary\n"
            f"/emergency_kill — sell everything NOW\n\n"
            f"<b>Market Maker:</b>\n"
            f"/mm_start &lt;mint&gt; — start MM on token\n"
            f"/mm_stop &lt;mint&gt; — stop MM on token\n"
            f"/mm_status — MM overview\n\n"
            f"<b>Info:</b>\n"
            f"/status — system status\n"
            f"/settings — current config\n\n"
            f"Auto-buy: <b>{'ON ✅' if self.auto_buy_enabled else 'OFF ⏸'}</b>\n"
            f"Mode: <b>{'DRY RUN 🧪' if DRY_RUN else 'LIVE 🔴'}</b>"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def autobuy_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        self.auto_buy_enabled = not self.auto_buy_enabled
        status = "ON ✅" if self.auto_buy_enabled else "OFF ⏸"
        await update.message.reply_text(f"🤖 Auto-buy: <b>{status}</b>", parse_mode=ParseMode.HTML)

    async def buy_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        if not context.args:
            await update.message.reply_text("Usage: /buy &lt;mint_address&gt; [amount_sol]", parse_mode=ParseMode.HTML)
            return
        mint = context.args[0]
        symbol = context.args[1] if len(context.args) > 1 else "UNKNOWN"
        await update.message.reply_text(f"⏳ Executing buy for <code>{mint}</code>...", parse_mode=ParseMode.HTML)
        if self.trader:
            result = await self.trader.execute_buy(mint, symbol)
            if result:
                await update.message.reply_text(f"✅ Buy executed. Position ID: <code>{result}</code>", parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text("❌ Buy failed. Check logs.")

    async def sell_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        if not context.args:
            await update.message.reply_text("Usage: /sell &lt;mint_address&gt;", parse_mode=ParseMode.HTML)
            return
        mint = context.args[0]
        fraction = float(context.args[1]) if len(context.args) > 1 else 1.0
        await update.message.reply_text(f"⏳ Selling <code>{mint}</code>...", parse_mode=ParseMode.HTML)
        if self.trader:
            result = await self.trader.execute_sell(mint, fraction)
            await update.message.reply_text("✅ Sell executed." if result else "❌ Sell failed.")

    async def positions_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        if not self.trader or not self.trader.positions:
            await update.message.reply_text("📭 No open positions.")
            return
        lines = ["📋 <b>Open Positions</b>\n"]
        for mint, pos in self.trader.positions.items():
            age = self._age(time.time() - pos.buy_time)
            pnl_pct = ((pos.current_price_sol - pos.entry_price_sol) / pos.entry_price_sol * 100) if pos.entry_price_sol else 0
            pnl_emoji = "🟢" if pnl_pct >= 0 else "🔴"
            lines.append(
                f"{pnl_emoji} <b>${pos.symbol}</b>\n"
                f"   Entry: {pos.entry_price_sol:.2f} SOL → Now: {pos.current_price_sol:.2f} SOL\n"
                f"   Size: {_sol(pos.amount_sol)} | PnL: <b>{_pct(pnl_pct)}</b> | Age: {age}\n"
                f"   <code>{mint[:8]}...{mint[-4:]}</code>"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

    async def pnl_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        if not self.trader:
            await update.message.reply_text("❌ Trader not available.")
            return
        total_invested = sum(p.amount_sol for p in self.trader.positions.values())
        total_value = sum(p.current_price_sol * (p.amount_sol / p.entry_price_sol) if p.entry_price_sol else 0
                         for p in self.trader.positions.values())
        pnl = total_value - total_invested
        pnl_pct = (pnl / total_invested * 100) if total_invested else 0
        emoji = "📈" if pnl >= 0 else "📉"
        text = (
            f"{emoji} <b>PnL Summary</b>\n\n"
            f"💼 Open Positions: <b>{len(self.trader.positions)}</b>\n"
            f"💰 Total Invested: <b>{_sol(total_invested)}</b>\n"
            f"💵 Current Value: <b>{_sol(total_value)}</b>\n"
            f"{'✅' if pnl >= 0 else '❌'} Unrealized PnL: <b>{_sol(pnl)}</b> (<b>{_pct(pnl_pct)}</b>)\n"
            f"⏰ {_now()}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        positions = len(self.trader.positions) if self.trader else 0
        text = (
            f"🖥 <b>System Status</b>\n\n"
            f"🤖 Auto-buy: <b>{'ON ✅' if self.auto_buy_enabled else 'OFF ⏸'}</b>\n"
            f"🔬 Mode: <b>{'DRY RUN 🧪' if DRY_RUN else 'LIVE 🔴'}</b>\n"
            f"📋 Open Positions: <b>{positions}</b>\n"
            f"🏦 MM Active: <b>{'Yes' if self.mm else 'No'}</b>\n"
            f"⏰ {_now()}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def settings_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        tp_str = ", ".join([f"+{int((m-1)*100)}%→{int(f*100)}%" for m, f in TAKE_PROFIT_LEVELS]) if TAKE_PROFIT_LEVELS else "none"
        text = (
            f"⚙️ <b>Current Config</b>\n\n"
            f"💰 Auto-buy amount: <b>{AUTO_BUY_AMOUNT_SOL} SOL</b>\n"
            f"🎯 Take profits: <b>{tp_str}</b>\n"
            f"🛑 Stop loss: <b>-{STOP_LOSS_PCT}%</b>\n"
            f"📊 Slippage: <b>{SLIPPAGE_BPS} bps</b>\n"
            f"🔬 Dry run: <b>{'YES 🧪' if DRY_RUN else 'NO 🔴'}</b>"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def emergency_kill_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        await update.message.reply_text("☠️ <b>EMERGENCY KILL ACTIVATING...</b>", parse_mode=ParseMode.HTML)
        positions_count = len(self.trader.positions) if self.trader else 0
        if self.trader:
            await self.trader.emergency_kill()
        if self.mm:
            await self.mm.emergency_kill()
        await self.notify_kill_switch(positions_count, 0.0)
        await update.message.reply_text("✅ Kill switch complete. All positions closed, MM stopped.")

    async def mm_start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update) or not self.mm: return
        if not context.args:
            await update.message.reply_text("Usage: /mm_start &lt;mint&gt;", parse_mode=ParseMode.HTML)
            return
        token = context.args[0]
        await self.mm.add_token(token)
        await update.message.reply_text(f"🏦 MM started on <code>{token}</code>", parse_mode=ParseMode.HTML)

    async def mm_stop_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update) or not self.mm: return
        if not context.args:
            await update.message.reply_text("Usage: /mm_stop &lt;mint&gt;", parse_mode=ParseMode.HTML)
            return
        token = context.args[0]
        await self.mm.remove_token(token)
        await update.message.reply_text(f"⏹ MM stopped on <code>{token}</code>", parse_mode=ParseMode.HTML)

    async def mm_status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update) or not self.mm: return
        status = self.mm.get_status()
        await update.message.reply_text(status)

    async def toggle_auto_buy_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Legacy alias for /autobuy"""
        await self.autobuy_cmd(update, context)

    def register_handlers(self, application: Application):
        application.add_handler(CommandHandler("start", self.start_cmd))
        application.add_handler(CommandHandler("autobuy", self.autobuy_cmd))
        application.add_handler(CommandHandler("toggle_auto_buy", self.toggle_auto_buy_cmd))
        application.add_handler(CommandHandler("buy", self.buy_cmd))
        application.add_handler(CommandHandler("sell", self.sell_cmd))
        application.add_handler(CommandHandler("positions", self.positions_cmd))
        application.add_handler(CommandHandler("pnl", self.pnl_cmd))
        application.add_handler(CommandHandler("status", self.status_cmd))
        application.add_handler(CommandHandler("settings", self.settings_cmd))
        application.add_handler(CommandHandler("emergency_kill", self.emergency_kill_cmd))
        application.add_handler(CommandHandler("mm_start", self.mm_start_cmd))
        application.add_handler(CommandHandler("mm_stop", self.mm_stop_cmd))
        application.add_handler(CommandHandler("mm_status", self.mm_status_cmd))
