#!/usr/bin/env python3
import asyncio
import threading
import sys
from detector import TrendingDetector
from signal_bot import SignalBot
from trader import TradeBot
from market_maker import MarketMaker
from web_dashboard.app import app, start_flask, detector, market_maker
from config import MM_TOKENS, TELEGRAM_BOT_TOKEN
from utils import logger
from telegram.ext import Application

async def main():
    # Initialize modules
    det = TrendingDetector()
    trader = TradeBot()
    mm = MarketMaker()
    signal = SignalBot(trader=trader, mm=mm)

    # Set Flask globals
    import web_dashboard.app as dash
    dash.detector = det
    dash.market_maker = mm

    # Start Telegram bot in its own asyncio task
    if TELEGRAM_BOT_TOKEN:
        app_telegram = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
        signal.register_handlers(app_telegram)
        asyncio.create_task(app_telegram.run_polling())

    # Connect detector WebSocket and register callbacks
    det.on_spike(lambda token: signal.send_spike(token))
    det.on_strong_signal(lambda token: trader.execute_buy(token.mint, token.symbol))

    # Start background tasks
    asyncio.create_task(det.connect())
    asyncio.create_task(trader.monitor_positions())

    # Start market making if tokens configured
    if MM_TOKENS:
        for mint in MM_TOKENS:
            await mm.add_token(mint.strip())
        asyncio.create_task(mm.run())

    # Start Flask in a daemon thread
    flask_thread = threading.Thread(target=start_flask, daemon=True)
    flask_thread.start()
    logger.info("Web dashboard running on http://localhost:5000")

    # Run until interrupt
    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    finally:
        await trader.emergency_kill()
        await mm.emergency_kill()
        # Close websocket, etc.
        logger.info("NoBrainTrade stopped.")

if __name__ == "__main__":
    asyncio.run(main())
