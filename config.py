import os
from dotenv import load_dotenv

load_dotenv()

# ── Telegram ──────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_SIGNAL_CHANNEL = os.getenv("TELEGRAM_SIGNAL_CHANNEL")
TELEGRAM_ADMIN_ID = os.getenv("TELEGRAM_ADMIN_ID")

# ── Solana RPC ────────────────────────────────────
SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# ── Admin Wallet (base58 private key) ────────────
PRIVATE_KEY = os.getenv("PRIVATE_KEY")

# ── Trading defaults ──────────────────────────────
AUTO_BUY_AMOUNT_SOL = float(os.getenv("AUTO_BUY_AMOUNT_SOL", "0.05"))
MAX_POSITION_SIZE_SOL = float(os.getenv("MAX_POSITION_SIZE_SOL", "0.5"))
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "3"))
SLIPPAGE_BPS = int(os.getenv("SLIPPAGE_BPS", "2500"))
STOP_LOSS_PCT = float(os.getenv("STOP_LOSS_PCT", "40"))
TAKE_PROFIT_LEVELS = [(2.0, 0.5), (3.0, 0.3), (5.0, 0.2)]

# ── Market Maker ──────────────────────────────────
MM_TOKENS = os.getenv("MM_TOKENS", "").split(",") if os.getenv("MM_TOKENS") else []
MM_SPREAD_BPS = int(os.getenv("MM_SPREAD_BPS", "20"))
MM_ORDER_SIZE_SOL = float(os.getenv("MM_ORDER_SIZE_SOL", "1.0"))
MM_MAX_INVENTORY_SOL = float(os.getenv("MM_MAX_INVENTORY_SOL", "5.0"))
MM_REBALANCE_THRESHOLD_SOL = float(os.getenv("MM_REBALANCE_THRESHOLD_SOL", "3.0"))
MM_UPDATE_INTERVAL_SEC = int(os.getenv("MM_UPDATE_INTERVAL_SEC", "5"))

# ── Spike Detector ────────────────────────────────
PUMP_PORTAL_WS = os.getenv("PUMP_PORTAL_WS", "wss://pumpportal.fun/api/data")
SPIKE_THRESHOLD_PCT = float(os.getenv("SPIKE_THRESHOLD_PCT", "150"))
MCAP_MIN_SOL = float(os.getenv("MCAP_MIN_SOL", "5"))
MCAP_MAX_SOL = float(os.getenv("MCAP_MAX_SOL", "500"))
SCAN_INTERVAL_SEC = int(os.getenv("SCAN_INTERVAL_SEC", "30"))

# ── Safety ────────────────────────────────────────
DRY_RUN = os.getenv("DRY_RUN", "true").lower() == "true"
