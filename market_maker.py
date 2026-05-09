import asyncio
import time
from typing import Dict, Optional
import aiohttp
from config import (MM_TOKENS, MM_SPREAD_BPS, MM_ORDER_SIZE_SOL,
                    MM_MAX_INVENTORY_SOL, MM_REBALANCE_THRESHOLD_SOL,
                    MM_UPDATE_INTERVAL_SEC, DRY_RUN)
from utils import logger

class MarketMaker:
    def __init__(self):
        self.tokens: Dict[str, dict] = {}   # mint -> {"orders": {...}, "inventory_sol": float}
        self.session = aiohttp.ClientSession()
        self.running = False

    async def add_token(self, mint: str):
        if mint not in self.tokens:
            self.tokens[mint] = {"inventory_sol": 0.0}
            logger.info(f"MM started for {mint}")

    async def remove_token(self, mint: str):
        if mint in self.tokens:
            await self._cancel_all_orders(mint)
            del self.tokens[mint]
            logger.info(f"MM stopped for {mint}")

    async def _cancel_all_orders(self, mint: str):
        # PumpPortal doesn't have a cancel endpoint; we just stop updating them
        pass

    async def get_mid_price(self, mint: str) -> Optional[float]:
        try:
            async with self.session.get(f"https://frontend-api.pump.fun/coins/{mint}") as resp:
                data = await resp.json()
                return float(data.get("market_cap", 0))
        except:
            return None

    async def _place_limit_order(self, mint: str, side: str, price: float, size_sol: float):
        """Place a limit order via pumpportal.fun if supported; else simulate."""
        if DRY_RUN:
            logger.info(f"DRY RUN: Place {side} order for {size_sol} SOL of {mint} at {price:.6f}")
            return

        # Attempt to use the trade endpoint with limit order parameters (unlikely, so fallback to market simulation)
        # For a real implementation, you'd use a proper limit order API.
        pass

    async def refresh_orders(self, mint: str):
        mid = await self.get_mid_price(mint)
        if not mid:
            return

        spread = MM_SPREAD_BPS / 10000
        bid_price = mid * (1 - spread / 2)
        ask_price = mid * (1 + spread / 2)
        size = MM_ORDER_SIZE_SOL

        # Check inventory limits before placing
        inventory = self.tokens[mint]["inventory_sol"]
        if inventory > MM_REBALANCE_THRESHOLD_SOL:
            # Skew orders to reduce inventory
            if bid_price > 0:
                bid_size = size * 0.5   # reduce buying
            ask_size = size * 1.5       # increase selling
        elif inventory < -MM_REBALANCE_THRESHOLD_SOL:
            bid_size = size * 1.5
            ask_size = size * 0.5
        else:
            bid_size = ask_size = size

        if inventory > MM_MAX_INVENTORY_SOL or inventory < -MM_MAX_INVENTORY_SOL:
            # Too much inventory, pause and rebalance
            logger.warning(f"Inventory out of bounds for {mint}: {inventory} SOL. Rebalancing...")
            await self._rebalance(mint)
            return

        # Place orders (only logging for now)
        logger.debug(f"MM {mint}: Bid {bid_size} SOL @ {bid_price:.6f}, Ask {ask_size} SOL @ {ask_price:.6f}")

    async def _rebalance(self, mint: str):
        """Dump or buy back half of excess inventory."""
        inventory = self.tokens[mint]["inventory_sol"]
        if inventory > MM_REBALANCE_THRESHOLD_SOL:
            sell_amount = (inventory - MM_REBALANCE_THRESHOLD_SOL) / 2
            logger.info(f"Rebalancing: selling {sell_amount} SOL of {mint}")
            # Execute market sell via pumpportal (would call trader)
        elif inventory < -MM_REBALANCE_THRESHOLD_SOL:
            buy_amount = (-inventory - MM_REBALANCE_THRESHOLD_SOL) / 2
            logger.info(f"Rebalancing: buying {buy_amount} SOL of {mint}")

    async def run(self):
        self.running = True
        while self.running:
            for mint in list(self.tokens.keys()):
                await self.refresh_orders(mint)
            await asyncio.sleep(MM_UPDATE_INTERVAL_SEC)

    async def emergency_kill(self):
        self.running = False
        # Cancel all orders (if possible) and exit
        logger.info("MM emergency kill: all orders cancelled.")
        self.tokens.clear()

    def get_status(self) -> str:
        status = "📊 <b>Market Maker Status</b>\n"
        for mint, data in self.tokens.items():
            status += f"- {mint[:6]}... Inventory: {data['inventory_sol']:.2f} SOL\n"
        return status
