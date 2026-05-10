import asyncio
import time
from datetime import datetime, timezone
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import Application, CommandHandler, ContextTypes
from config import (TELEGRAM_BOT_TOKEN, TELEGRAM_SIGNAL_CHANNEL,
                    TELEGRAM_ADMIN_ID, DRY_RUN, AUTO_BUY_AMOUNT_SOL,
                    STOP_LOSS_PCT, TAKE_PROFIT_LEVELS, SLIPPAGE_BPS)
from utils import logger


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

def _sol(val):
    return f"{val:.4f} SOL"

def _pct(val):
    return f"{'+' if val >= 0 else ''}{val:.1f}%"


class SignalBot:
    def __init__(self, trader=None, mm=None):
        self.bot = Bot(token=TELEGRAM_BOT_TOKEN)
        self.trader = trader
        self.mm = mm
        self.auto_buy_enabled = True
        self._wallet_manager = None

    def set_wallet_manager(self, wm):
        self._wallet_manager = wm

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
        await self._admin(
            f"🧠 Strong signal: <b>${token.symbol}</b> score {score}/100 — "
            f"auto-buy {'queued' if self.auto_buy_enabled else 'OFF'}"
        )

    # ── Trade notifications ────────────────────────────────────────────────

    async def notify_buy_executed(self, token_symbol, mint, amount_sol, mcap_sol, position_id, dry=False):
        tp_str = " | ".join([f"+{int((m-1)*100)}%→{int(f*100)}%" for m, f in TAKE_PROFIT_LEVELS]) if TAKE_PROFIT_LEVELS else "—"
        wallet_info = ""
        if self._wallet_manager:
            w = self._wallet_manager.get_active_wallet()
            if w:
                wallet_info = f"👛 Wallet: <code>{w['pubkey'][:8]}...{w['pubkey'][-4:]}</code> (#{w['index']})\n"
        text = (
            f"{'🧪 DRY RUN — ' if dry else ''}🟢 <b>BUY EXECUTED</b>\n\n"
            f"🤖 Auto-buy\n"
            f"🪙 <b>${token_symbol}</b>\n"
            f"📍 <code>{mint}</code>\n"
            f"{wallet_info}"
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

    async def notify_sell_executed(self, token_symbol, mint, amount_sol, reason, pnl_sol, pnl_pct, dry=False):
        pnl_emoji = "✅" if pnl_sol >= 0 else "❌"
        reason_map = {
            "tp": "🎯 Take Profit Hit",
            "sl": "🛑 Stop Loss Hit",
            "kill": "☠️ Emergency Kill",
            "manual": "👤 Manual Sell",
        }
        text = (
            f"{'🧪 DRY RUN — ' if dry else ''}🔴 <b>SELL EXECUTED</b>\n\n"
            f"{reason_map.get(reason, reason)}\n"
            f"🪙 <b>${token_symbol}</b>\n"
            f"📍 <code>{mint}</code>\n"
            f"💰 Sold: <b>{_sol(amount_sol)}</b>\n\n"
            f"{pnl_emoji} PnL: <b>{_sol(pnl_sol)}</b> (<b>{_pct(pnl_pct)}</b>)\n"
            f"⏰ {_now()}"
        )
        await self._admin(text)

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

    async def notify_stop_loss(self, token_symbol, mint, drawdown_pct, pnl_sol):
        text = (
            f"🛑 <b>STOP LOSS TRIGGERED</b>\n\n"
            f"🪙 <b>${token_symbol}</b>\n"
            f"📉 Drawdown: <b>{_pct(-drawdown_pct)}</b>\n"
            f"💸 Loss: <b>{_sol(pnl_sol)}</b>\n"
            f"⏰ {_now()}"
        )
        await self._admin(text)

    async def notify_kill_switch(self, positions_closed, total_pnl):
        text = (
            f"☠️ <b>EMERGENCY KILL SWITCH ACTIVATED</b>\n\n"
            f"📤 Positions closed: <b>{positions_closed}</b>\n"
            f"💰 Total PnL: <b>{_sol(total_pnl)}</b>\n"
            f"⏰ {_now()}"
        )
        await self._admin(text)

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

    def _is_admin(self, update: Update) -> bool:
        return str(update.effective_user.id) == str(TELEGRAM_ADMIN_ID)

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
        if seconds < 60: return f"{int(seconds)}s"
        if seconds < 3600: return f"{int(seconds/60)}m"
        return f"{int(seconds/3600)}h"

    # ── Command Handlers ───────────────────────────────────────────────────

    async def start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        text = (
            f"🧠 <b>No Brain Trade — Admin Panel</b>\n\n"
            f"<b>💼 Trading:</b>\n"
            f"/autobuy — toggle auto-buy\n"
            f"/buy &lt;mint&gt; — manual buy\n"
            f"/sell &lt;mint&gt; — manual sell\n"
            f"/positions — open positions\n"
            f"/pnl — profit &amp; loss\n"
            f"/emergency_kill — sell everything\n\n"
            f"<b>👛 Wallets:</b>\n"
            f"/newwallet — create new wallet\n"
            f"/wallets — list all wallets\n"
            f"/exportwallet &lt;n&gt; — export private key\n"
            f"/setwallet &lt;n&gt; — set active wallet\n\n"
            f"<b>🏦 Market Maker:</b>\n"
            f"/mm_start &lt;mint&gt;\n"
            f"/mm_stop &lt;mint&gt;\n"
            f"/mm_status\n\n"
            f"<b>ℹ️ Info:</b>\n"
            f"/status — system status\n"
            f"/settings — current config\n\n"
            f"Auto-buy: <b>{'ON ✅' if self.auto_buy_enabled else 'OFF ⏸'}</b>\n"
            f"Mode: <b>{'DRY RUN 🧪' if DRY_RUN else 'LIVE 🔴'}</b>"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def newwallet_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        if not self._wallet_manager:
            await update.message.reply_text("❌ Wallet manager not available.")
            return
        user = update.effective_user
        wallet = self._wallet_manager.create_wallet(
            username=user.username or "",
            user_id=str(user.id),
            display_name=user.full_name or f"Wallet #{len(self._wallet_manager.wallets) + 1}"
        )
        text = self._wallet_manager.format_create_notification(wallet)
        await self._admin(text)
        await update.message.reply_text(
            f"✅ Wallet <b>#{wallet['index']}</b> created.\n"
            f"📍 <code>{wallet['pubkey']}</code>\n\n"
            f"⚠️ Private key sent to your DM.",
            parse_mode=ParseMode.HTML
        )

    async def wallets_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        if not self._wallet_manager:
            await update.message.reply_text("❌ Wallet manager not available.")
            return
        balances = {}
        for w in self._wallet_manager.wallets:
            bal = await self._wallet_manager.get_balance(w["pubkey"])
            balances[w["pubkey"]] = bal
        text = self._wallet_manager.format_wallet_list(balances)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def exportwallet_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        if not self._wallet_manager:
            await update.message.reply_text("❌ Wallet manager not available.")
            return
        if not context.args:
            await update.message.reply_text("Usage: /exportwallet &lt;number&gt;", parse_mode=ParseMode.HTML)
            return
        try:
            index = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Invalid number.")
            return
        wallet = self._wallet_manager.get_wallet(index)
        if not wallet:
            await update.message.reply_text(f"❌ Wallet #{index} not found.")
            return
        text = (
            f"🔑 <b>Wallet #{wallet['index']} Export</b>\n\n"
            f"📍 Public Key:\n<code>{wallet['pubkey']}</code>\n\n"
            f"🔐 Private Key:\n<code>{wallet['privkey']}</code>\n\n"
            f"⚠️ <b>Never share your private key.</b>\n"
            f"⏰ {_now()}"
        )
        await self._admin(text)
        await update.message.reply_text("✅ Private key sent to your DM.")

    async def setwallet_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        if not self._wallet_manager:
            await update.message.reply_text("❌ Wallet manager not available.")
            return
        if not context.args:
            await update.message.reply_text("Usage: /setwallet &lt;number&gt;", parse_mode=ParseMode.HTML)
            return
        try:
            index = int(context.args[0])
        except ValueError:
            await update.message.reply_text("❌ Invalid number.")
            return
        if self._wallet_manager.set_active(index):
            w = self._wallet_manager.get_wallet(index)
            await update.message.reply_text(
                f"✅ Active wallet → <b>#{index}</b>\n<code>{w['pubkey']}</code>",
                parse_mode=ParseMode.HTML
            )
            await self._admin(
                f"👛 <b>Active Wallet Changed</b>\n\n"
                f"🪪 #{index}\n"
                f"📍 <code>{w['pubkey']}</code>\n"
                f"⏰ {_now()}"
            )
        else:
            await update.message.reply_text(f"❌ Wallet #{index} not found.")

    async def autobuy_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        self.auto_buy_enabled = not self.auto_buy_enabled
        await update.message.reply_text(
            f"🤖 Auto-buy: <b>{'ON ✅' if self.auto_buy_enabled else 'OFF ⏸'}</b>",
            parse_mode=ParseMode.HTML
        )

    async def buy_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        if not context.args:
            await update.message.reply_text("Usage: /buy &lt;mint&gt; [symbol]", parse_mode=ParseMode.HTML)
            return
        mint = context.args[0]
        symbol = context.args[1] if len(context.args) > 1 else "UNKNOWN"
        await update.message.reply_text(f"⏳ Buying <code>{mint}</code>...", parse_mode=ParseMode.HTML)
        if self.trader:
            result = await self.trader.execute_buy(mint, symbol)
            if result:
                await update.message.reply_text(f"✅ Bought. Position: <code>{result}</code>", parse_mode=ParseMode.HTML)
            else:
                await update.message.reply_text("❌ Buy failed.")

    async def sell_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        if not context.args:
            await update.message.reply_text("Usage: /sell &lt;mint&gt; [fraction]", parse_mode=ParseMode.HTML)
            return
        mint = context.args[0]
        fraction = float(context.args[1]) if len(context.args) > 1 else 1.0
        await update.message.reply_text(f"⏳ Selling...", parse_mode=ParseMode.HTML)
        if self.trader:
            result = await self.trader.execute_sell(mint, fraction)
            await update.message.reply_text("✅ Sold." if result else "❌ Sell failed.")

    async def positions_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        if not self.trader or not self.trader.positions:
            await update.message.reply_text("📭 No open positions.")
            return
        lines = ["📋 <b>Open Positions</b>\n"]
        for mint, pos in self.trader.positions.items():
            age = self._age(time.time() - pos.buy_time)
            pnl_pct = pos.pnl_pct()
            emoji = "🟢" if pnl_pct >= 0 else "🔴"
            lines.append(
                f"{emoji} <b>${pos.symbol}</b>\n"
                f"   Entry: {pos.entry_price_sol:.2f} → Now: {pos.current_price_sol:.2f} SOL\n"
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
        total_value = sum(
            p.current_price_sol * (p.amount_sol / p.entry_price_sol) if p.entry_price_sol else 0
            for p in self.trader.positions.values()
        )
        pnl = total_value - total_invested
        pnl_pct = (pnl / total_invested * 100) if total_invested else 0
        realized = getattr(self.trader, '_total_realized_pnl', 0.0)
        text = (
            f"{'📈' if pnl >= 0 else '📉'} <b>PnL Summary</b>\n\n"
            f"💼 Open Positions: <b>{len(self.trader.positions)}</b>\n"
            f"💰 Invested: <b>{_sol(total_invested)}</b>\n"
            f"💵 Current Value: <b>{_sol(total_value)}</b>\n"
            f"{'✅' if pnl >= 0 else '❌'} Unrealized: <b>{_sol(pnl)}</b> ({_pct(pnl_pct)})\n"
            f"💸 Realized: <b>{_sol(realized)}</b>\n"
            f"⏰ {_now()}"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    async def status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update): return
        positions = len(self.trader.positions) if self.trader else 0
        wallets = len(self._wallet_manager.wallets) if self._wallet_manager else 0
        active_w = self._wallet_manager.active_index if self._wallet_manager else "—"
        text = (
            f"🖥 <b>System Status</b>\n\n"
            f"🤖 Auto-buy: <b>{'ON ✅' if self.auto_buy_enabled else 'OFF ⏸'}</b>\n"
            f"🔬 Mode: <b>{'DRY RUN 🧪' if DRY_RUN else 'LIVE 🔴'}</b>\n"
            f"📋 Open Positions: <b>{positions}</b>\n"
            f"👛 Wallets: <b>{wallets}</b> (active: #{active_w})\n"
            f"🏦 MM: <b>{'Active' if self.mm else 'Disabled'}</b>\n"
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
        await update.message.reply_text("☠️ <b>KILL SWITCH ACTIVATING...</b>", parse_mode=ParseMode.HTML)
        positions_count = len(self.trader.positions) if self.trader else 0
        if self.trader:
            await self.trader.emergency_kill()
        if self.mm:
            await self.mm.emergency_kill()
        await self.notify_kill_switch(positions_count, 0.0)
        await update.message.reply_text("✅ Done. All positions closed.")

    async def mm_start_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update) or not self.mm: return
        if not context.args:
            await update.message.reply_text("Usage: /mm_start &lt;mint&gt;", parse_mode=ParseMode.HTML)
            return
        await self.mm.add_token(context.args[0])
        await update.message.reply_text(f"🏦 MM started on <code>{context.args[0]}</code>", parse_mode=ParseMode.HTML)

    async def mm_stop_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update) or not self.mm: return
        if not context.args:
            await update.message.reply_text("Usage: /mm_stop &lt;mint&gt;", parse_mode=ParseMode.HTML)
            return
        await self.mm.remove_token(context.args[0])
        await update.message.reply_text(f"⏹ MM stopped on <code>{context.args[0]}</code>", parse_mode=ParseMode.HTML)

    async def mm_status_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_admin(update) or not self.mm: return
        await update.message.reply_text(self.mm.get_status())

    async def toggle_auto_buy_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
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
        application.add_handler(CommandHandler("newwallet", self.newwallet_cmd))
        application.add_handler(CommandHandler("wallets", self.wallets_cmd))
        application.add_handler(CommandHandler("exportwallet", self.exportwallet_cmd))
        application.add_handler(CommandHandler("setwallet", self.setwallet_cmd))
        application.add_handler(CommandHandler("mm_start", self.mm_start_cmd))
        application.add_handler(CommandHandler("mm_stop", self.mm_stop_cmd))
        application.add_handler(CommandHandler("mm_status", self.mm_status_cmd))
