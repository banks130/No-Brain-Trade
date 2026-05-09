#!/usr/bin/env python3
import asyncio
import threading
import signal
import sys
import time

from detector import TrendingDetector
from signal_bot import SignalBot
from trader import TradeBot
from market_maker import MarketMaker
from web_dashboard.app import app, start_flask
from config import MM_TOKENS, TELEGRAM_BOT_TOKEN
from utils import logger
from telegram.ext import Application

# Global references for shutdown
trader = None
market_maker = None
detector = None
telegram_app = None
tasks = []

async def main():
    global trader, market_maker, detector, telegram_app, tasks

    # ── 1. Modules ──────────────────────────────────
    detector = TrendingDetector()
    trader = TradeBot()
    market_maker = MarketMaker()
    signal_bot = SignalBot(trader=trader, mm=market_maker)

    # Inject into Flask globals
    import web_dashboard.app as dash
    dash.detector = detector
    dash.market_maker = market_maker

    # ── 2. Telegram bot setup ───────────────────────
    if TELEGRAM_BOT_TOKEN:
        telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        signal_bot.register_handlers(telegram_app)
        await telegram_app.initialize()
        await telegram_app.start()
        asyncio.create_task(telegram_app.updater.start_polling())
        logger.info("Telegram bot started")
    else:
        logger.warning("Telegram token not set – bot disabled")

    # ── 3. Detector callbacks ───────────────────────
    async def on_spike(token):
        await signal_bot.send_spike(token)

    async def on_strong_signal(token):
        if signal_bot.auto_buy_enabled:
            await trader.execute_buy(token.mint, token.symbol)

    detector.on_spike(on_spike)
    detector.on_strong_signal(on_strong_signal)

    # ── 4. Start Flask in a daemon thread ─────────
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()
    logger.info("Web dashboard starting on http://0.0.0.0:5000")
    time.sleep(2)  # give Waitress time to bind

    # ── 5. Background tasks ─────────────────────────
    tasks = [
        asyncio.create_task(detector.connect()),
        asyncio.create_task(trader.monitor_positions()),
    ]

    # Market making
    if MM_TOKENS:
        for mint in MM_TOKENS:
            mint = mint.strip()
            if mint:
                await market_maker.add_token(mint)
        tasks.append(asyncio.create_task(market_maker.run()))

    # ── 6. Wait until shutdown signal ───────────────
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def shutdown():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop.add_signal_handler(signal.SIGTERM, shutdown)
    loop.add_signal_handler(signal.SIGINT, shutdown)

    await stop_event.wait()

    # ── 7. Clean shutdown ───────────────────────────
    logger.info("Shutting down all modules…")
    if trader:
        await trader.emergency_kill()
    if market_maker:
        await market_maker.emergency_kill()

    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)

    if telegram_app:
        await telegram_app.stop()
        await telegram_app.shutdown()

    logger.info("NoBrainTrade stopped cleanly.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
