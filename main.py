#!/usr/bin/env python3
import threading
import time
import os
import asyncio
import traceback

from web_dashboard.app import start_flask
from utils import logger

def run_trading_engine():
    """Run the async trading components in a dedicated event loop inside a daemon thread."""
    try:
        asyncio.run(trading_main())
    except Exception as e:
        logger.error(f"Trading engine crashed: {e}")
        traceback.print_exc()

async def trading_main():
    # Import modules here so they don't interfere with Flask startup
    from detector import TrendingDetector
    from signal_bot import SignalBot
    from trader import TradeBot
    from market_maker import MarketMaker
    from config import MM_TOKENS, TELEGRAM_BOT_TOKEN
    from telegram.ext import Application

    logger.info("Trading engine starting...")

    # Initialize components
    detector = TrendingDetector()
    trader_obj = TradeBot()
    market_maker = MarketMaker()
    signal_bot = SignalBot(trader=trader_obj, mm=market_maker)

    # Inject into Flask globals (so API routes work)
    import web_dashboard.app as dash
    dash.detector = detector
    dash.market_maker = market_maker

    # Telegram bot (start safely, ignore duplicate instance errors)
    if TELEGRAM_BOT_TOKEN:
        try:
            telegram_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
            signal_bot.register_handlers(telegram_app)
            await telegram_app.initialize()
            await telegram_app.start()
            asyncio.create_task(telegram_app.updater.start_polling())
            logger.info("Telegram bot started")
        except Exception as e:
            from telegram.error import Conflict
            if isinstance(e, Conflict):
                logger.warning("Telegram Conflict – another instance is polling. Skipping bot.")
            else:
                logger.error(f"Telegram bot failed: {e}")
    else:
        logger.warning("Telegram token missing – bot disabled")

    # Callbacks
    async def on_spike(token):
        try:
            await signal_bot.send_spike(token)
        except Exception:
            pass

    async def on_strong_signal(token):
        try:
            if signal_bot.auto_buy_enabled:
                await trader_obj.execute_buy(token.mint, token.symbol)
        except Exception:
            pass

    detector.on_spike(on_spike)
    detector.on_strong_signal(on_strong_signal)

    # Background tasks
    tasks = [
        asyncio.create_task(detector.connect()),
        asyncio.create_task(trader_obj.monitor_positions()),
    ]

    if MM_TOKENS:
        for mint in MM_TOKENS:
            mint = mint.strip()
            if mint:
                await market_maker.add_token(mint)
        tasks.append(asyncio.create_task(market_maker.run()))

    logger.info("All trading modules started.")
    logger.info("NoBrainTrade is LIVE.")

    # Wait forever (until tasks complete or exception)
    await asyncio.gather(*tasks)

# ── Main entry point ─────────────────────────
if __name__ == "__main__":
    # 1. Start the web server (Flask) in a daemon thread
    flask_port = int(os.environ.get("PORT", 8080))
    flask_thread = threading.Thread(
        target=start_flask,
        args=("0.0.0.0", flask_port),
        daemon=True
    )
    flask_thread.start()
    time.sleep(2)  # let Waitress bind

    logger.info(f"Web dashboard running on port {flask_port}")
    logger.info("Health check ready at /health")

    # 2. Start the trading engine in another daemon thread
    trading_thread = threading.Thread(target=run_trading_engine, daemon=True)
    trading_thread.start()

    # 3. Keep the main process alive (so Railway doesn't kill the container)
    while True:
        time.sleep(3600)
