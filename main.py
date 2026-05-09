#!/usr/bin/env python3
import asyncio
import threading
import signal
import sys
import time
import os
import traceback

from detector import TrendingDetector
from signal_bot import SignalBot
from trader import TradeBot
from market_maker import MarketMaker
from web_dashboard.app import app, start_flask
from config import MM_TOKENS, TELEGRAM_BOT_TOKEN
from utils import logger
from telegram.ext import Application

trader = None
market_maker = None
detector = None
telegram_app = None
tasks = []

async def main():
    global trader, market_maker, detector, telegram_app, tasks

    logger.info("=" * 50)
    logger.info("NoBrainTrade starting...")

    # ── 1. Start Flask FIRST (before any async work) ──
    flask_port = int(os.environ.get("PORT", 5000))
    flask_ready = threading.Event()
    
    def run_flask():
        try:
            logger.info(f"Flask starting on 0.0.0.0:{flask_port}")
            flask_ready.set()  # signal that we're about to start
            start_flask("0.0.0.0", flask_port)
        except Exception as e:
            logger.error(f"Flask crashed: {e}")
            traceback.print_exc()
    
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    
    # Wait for Flask to actually bind
    flask_ready.wait()
    time.sleep(2)  # extra buffer for Waitress to start
    logger.info(f"Flask should now be listening on port {flask_port}")

    # ── 2. Core modules (lightweight, no network yet) ──
    detector = TrendingDetector()
    trader = TradeBot()
    market_maker = MarketMaker()
    signal_bot = SignalBot(trader=trader, mm=market_maker)

    import web_dashboard.app as dash
    dash.detector = detector
    dash.market_maker = market_maker

    # ── 3. Telegram bot (can fail gracefully) ────────
    if TELEGRAM_BOT_TOKEN:
        try:
            telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
            signal_bot.register_handlers(telegram_app)
            await telegram_app.initialize()
            await telegram_app.start()
            asyncio.create_task(telegram_app.updater.start_polling())
            logger.info("Telegram bot started")
        except Exception as e:
            logger.error(f"Telegram bot failed: {e}")
    else:
        logger.warning("Telegram token missing – bot disabled")

    # ── 4. Detector callbacks ──────────────────────
    async def on_spike(token):
        try:
            await signal_bot.send_spike(token)
        except Exception:
            pass

    async def on_strong_signal(token):
        try:
            if signal_bot.auto_buy_enabled:
                await trader.execute_buy(token.mint, token.symbol)
        except Exception:
            pass

    detector.on_spike(on_spike)
    detector.on_strong_signal(on_strong_signal)

    # ── 5. Background tasks (start after Flask is ready) ──
    tasks = [
        asyncio.create_task(detector.connect()),
        asyncio.create_task(trader.monitor_positions()),
    ]

    if MM_TOKENS:
        for mint in MM_TOKENS:
            mint = mint.strip()
            if mint:
                await market_maker.add_token(mint)
        tasks.append(asyncio.create_task(market_maker.run()))

    logger.info("=" * 50)
    logger.info("All modules started. NoBrainTrade is LIVE.")
    logger.info(f"Dashboard: http://0.0.0.0:{flask_port}")
    logger.info("=" * 50)

    # ── 6. Wait for shutdown ──────────────────────
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def shutdown():
        logger.info("Shutdown signal received")
        stop_event.set()

    loop.add_signal_handler(signal.SIGTERM, shutdown)
    loop.add_signal_handler(signal.SIGINT, shutdown)

    await stop_event.wait()

    # ── 7. Clean shutdown ─────────────────────────
    logger.info("Shutting down…")
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

    logger.info("NoBrainTrade stopped.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        traceback.print_exc()
        sys.exit(1)
