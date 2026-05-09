import asyncio
import time
from typing import Dict, Optional
import aiohttp
from dataclasses import dataclass, field
from solders.keypair import Keypair
from config import (PRIVATE_KEY, AUTO_BUY_AMOUNT_SOL, MAX_POSITION_SIZE_SOL,
                    MAX_CONCURRENT_POSITIONS, SLIPPAGE_BPS, STOP_LOSS_PCT,
                    TAKE_PROFIT_LEVELS, DRY_RUN, MCAP_MAX_SOL)
from utils import logger

@dataclass
class Position:
    token_mint: str
    symbol: str
    entry_price_sol: float
    amount_sol: float
    current_price_sol: float = 0.0
    highest_price_sol: float = 0.0
    buy_time: float = field(default_factory=time.time)
    tp_levels: list = field(default_factory=list)
    id: str = field(default_factory=lambda: hex(int(time.time()*1000))[2:])

class TradeBot:
    def __init__(self, signal_bot=None):
        self.keypair = Keypair.from_base58_string(PRIVATE_KEY) if PRIVATE_KEY and not DRY_RUN else None
        self.positions: Dict[str, Position] = {}
        self.session = aiohttp.ClientSession()
        self.signal_bot = signal_bot
        self.auto_buy = True

    async def get_token_price(self, mint: str) -> Optional[float]:
        try:
            async with self.session.get(f"https://frontend-api.pump.fun/coins/{mint}") as resp:
                data = await resp.json()
                return float(data.get("market_cap", 0))
        except:
            return None

    async def execute_buy(self, mint: str, symbol: str) -> Optional[str]:
        if len(self.positions) >= MAX_CONCURRENT_POSITIONS:
            return None

        price_sol = await self.get_token_price(mint)
        if price_sol and price_sol > MCAP_MAX_SOL:
            return None

        amount = min(AUTO_BUY_AMOUNT_SOL, MAX_POSITION_SIZE_SOL)
        if DRY_RUN:
            logger.info(f"DRY RUN: Would buy {amount} SOL of {symbol} ({mint})")
            pos = Position(mint, symbol, price_sol or 0, amount)
            self.positions[mint] = pos
            return pos.id

        payload = {
            "action": "buy",
            "mint": mint,
            "amount": amount,
            "denominatedInSol": "true",
            "slippage": SLIPPAGE_BPS,
            "priorityFee": 0.005,
            "privateKey": self.keypair.to_base58_string(),
        }
        async with self.session.post("https://pumpportal.fun/api/trade", json=payload) as resp:
            data = await resp.json()
            if data.get("error"):
                logger.error(f"Buy error: {data}")
                return None
        pos = Position(mint, symbol, price_sol or 0, amount)
        self.positions[mint] = pos
        msg = f"🟢 Bought {amount} SOL of {symbol} (MCap {price_sol:.2f})"
        logger.info(msg)
        if self.signal_bot:
            await self.signal_bot.send_admin_log(msg)
        return pos.id

    async def execute_sell(self, mint: str, fraction: float = 1.0) -> bool:
        if mint not in self.positions:
            return False
        pos = self.positions[mint]
        amount = pos.amount_sol * fraction
        if DRY_RUN:
            logger.info(f"DRY RUN: Would sell {amount} SOL of {pos.symbol}")
            pos.amount_sol -= amount
            if pos.amount_sol <= 0.0001:
                del self.positions[mint]
            return True

        payload = {
            "action": "sell",
            "mint": mint,
            "amount": amount,
            "denominatedInSol": "true",
            "slippage": SLIPPAGE_BPS,
            "privateKey": self.keypair.to_base58_string(),
        }
        async with self.session.post("https://pumpportal.fun/api/trade", json=payload) as resp:
            data = await resp.json()
            if data.get("error"):
                logger.error(f"Sell error: {data}")
                return False
        pos.amount_sol -= amount
        msg = f"🔴 Sold {amount} SOL of {pos.symbol}"
        logger.info(msg)
        if self.signal_bot:
            await self.signal_bot.send_admin_log(msg)
        if pos.amount_sol <= 0.0001:
            del self.positions[mint]
        return True

    async def monitor_positions(self):
        while True:
            for mint, pos in list(self.positions.items()):
                price = await self.get_token_price(mint)
                if not price:
                    continue
                pos.current_price_sol = price
                if price > pos.highest_price_sol:
                    pos.highest_price_sol = price

                # Stop loss
                if pos.highest_price_sol > 0:
                    drawdown = (pos.highest_price_sol - price) / pos.highest_price_sol * 100
                    if drawdown >= STOP_LOSS_PCT:
                        logger.info(f"SL triggered for {pos.symbol}")
                        await self.execute_sell(mint, 1.0)
                        continue

                # Take profit
                for mult, frac in list(pos.tp_levels):
                    if price >= pos.entry_price_sol * mult:
                        await self.execute_sell(mint, frac)
                        pos.tp_levels.remove((mult, frac))
                        break
            await asyncio.sleep(5)

    async def emergency_kill(self):
        logger.warning("KILL SWITCH: Selling all positions")
        for mint in list(self.positions.keys()):
            await self.execute_sell(mint, 1.0)
