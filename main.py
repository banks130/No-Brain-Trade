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
