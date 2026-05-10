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
    realized_pnl: float = 0.0
    id: str = field(default_factory=lambda: hex(int(time.time() * 1000))[2:])

    def pnl_sol(self) -> float:
        if self.entry_price_sol <= 0:
            return 0.0
        ratio = self.current_price_sol / self.entry_price_sol
        return (ratio - 1) * self.amount_sol

    def pnl_pct(self) -> float:
        if self.entry_price_sol <= 0:
            return 0.0
        return (self.current_price_sol - self.entry_price_sol) / self.entry_price_sol * 100


class TradeBot:
    def __init__(self, signal_bot=None):
        self.keypair = Keypair.from_base58_string(PRIVATE_KEY) if PRIVATE_KEY and not DRY_RUN else None
        self.positions: Dict[str, Position] = {}
        self.session = aiohttp.ClientSession()
        self.signal_bot = signal_bot
        self.auto_buy = True
        self._total_realized_pnl = 0.0

    def set_signal_bot(self, signal_bot):
        self.signal_bot = signal_bot

    async def get_token_price(self, mint: str) -> Optional[float]:
        try:
            async with self.session.get(
                f"https://frontend-api.pump.fun/coins/{mint}",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                data = await resp.json()
                return float(data.get("market_cap", 0))
        except Exception as e:
            logger.warning(f"Price fetch failed for {mint}: {e}")
            return None

    async def execute_buy(self, mint: str, symbol: str) -> Optional[str]:
        if len(self.positions) >= MAX_CONCURRENT_POSITIONS:
            logger.info(f"Max positions reached, skipping {symbol}")
            return None

        if mint in self.positions:
            logger.info(f"Already in position for {symbol}")
            return None

        price_sol = await self.get_token_price(mint)
        if price_sol and price_sol > MCAP_MAX_SOL:
            logger.info(f"MCap too high for {symbol}: {price_sol}")
            return None

        amount = min(AUTO_BUY_AMOUNT_SOL, MAX_POSITION_SIZE_SOL)

        # Build TP levels from config
        tp_levels = list(TAKE_PROFIT_LEVELS) if TAKE_PROFIT_LEVELS else []

        if DRY_RUN:
            logger.info(f"DRY RUN: Would buy {amount} SOL of {symbol} ({mint})")
            pos = Position(
                token_mint=mint,
                symbol=symbol,
                entry_price_sol=price_sol or 0,
                amount_sol=amount,
                current_price_sol=price_sol or 0,
                highest_price_sol=price_sol or 0,
                tp_levels=tp_levels,
            )
            self.positions[mint] = pos
            if self.signal_bot:
                await self.signal_bot.notify_buy_executed(
                    symbol, mint, amount, price_sol or 0, pos.id, dry=True
                )
            return pos.id

        try:
            payload = {
                "action": "buy",
                "mint": mint,
                "amount": amount,
                "denominatedInSol": "true",
                "slippage": SLIPPAGE_BPS,
                "priorityFee": 0.005,
                "privateKey": self.keypair.to_base58_string(),
            }
            async with self.session.post(
                "https://pumpportal.fun/api/trade",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()
                if data.get("error"):
                    logger.error(f"Buy API error for {symbol}: {data}")
                    if self.signal_bot:
                        await self.signal_bot.notify_error(f"Buy {symbol}", str(data.get("error")))
                    return None

            pos = Position(
                token_mint=mint,
                symbol=symbol,
                entry_price_sol=price_sol or 0,
                amount_sol=amount,
                current_price_sol=price_sol or 0,
                highest_price_sol=price_sol or 0,
                tp_levels=tp_levels,
            )
            self.positions[mint] = pos
            logger.info(f"✅ Bought {amount} SOL of {symbol}")

            if self.signal_bot:
                await self.signal_bot.notify_buy_executed(
                    symbol, mint, amount, price_sol or 0, pos.id, dry=False
                )
            return pos.id

        except Exception as e:
            logger.error(f"Buy exception for {symbol}: {e}")
            if self.signal_bot:
                await self.signal_bot.notify_error(f"Buy {symbol}", str(e))
            return None

    async def execute_sell(self, mint: str, fraction: float = 1.0, reason: str = "manual") -> bool:
        if mint not in self.positions:
            return False

        pos = self.positions[mint]
        amount = pos.amount_sol * fraction
        pnl = pos.pnl_sol() * fraction
        pnl_pct = pos.pnl_pct()

        if DRY_RUN:
            logger.info(f"DRY RUN: Would sell {amount} SOL of {pos.symbol} ({reason})")
            pos.amount_sol -= amount
            pos.realized_pnl += pnl
            self._total_realized_pnl += pnl
            if pos.amount_sol <= 0.0001:
                del self.positions[mint]
            if self.signal_bot:
                await self.signal_bot.notify_sell_executed(
                    pos.symbol, mint, amount, reason, pnl, pnl_pct, dry=True
                )
            return True

        try:
            payload = {
                "action": "sell",
                "mint": mint,
                "amount": amount,
                "denominatedInSol": "true",
                "slippage": SLIPPAGE_BPS,
                "privateKey": self.keypair.to_base58_string(),
            }
            async with self.session.post(
                "https://pumpportal.fun/api/trade",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                data = await resp.json()
                if data.get("error"):
                    logger.error(f"Sell API error for {pos.symbol}: {data}")
                    if self.signal_bot:
                        await self.signal_bot.notify_error(f"Sell {pos.symbol}", str(data.get("error")))
                    return False

            pos.amount_sol -= amount
            pos.realized_pnl += pnl
            self._total_realized_pnl += pnl
            logger.info(f"✅ Sold {amount} SOL of {pos.symbol} ({reason})")

            if self.signal_bot:
                await self.signal_bot.notify_sell_executed(
                    pos.symbol, mint, amount, reason, pnl, pnl_pct, dry=False
                )

            if pos.amount_sol <= 0.0001:
                del self.positions[mint]
            return True

        except Exception as e:
            logger.error(f"Sell exception for {pos.symbol}: {e}")
            if self.signal_bot:
                await self.signal_bot.notify_error(f"Sell {pos.symbol}", str(e))
            return False

    async def monitor_positions(self):
        while True:
            for mint, pos in list(self.positions.items()):
                try:
                    price = await self.get_token_price(mint)
                    if not price:
                        continue

                    pos.current_price_sol = price
                    if price > pos.highest_price_sol:
                        pos.highest_price_sol = price

                    # Stop loss check
                    if pos.highest_price_sol > 0:
                        drawdown = (pos.highest_price_sol - price) / pos.highest_price_sol * 100
                        if drawdown >= STOP_LOSS_PCT:
                            logger.info(f"SL triggered for {pos.symbol} ({drawdown:.1f}% drawdown)")
                            pnl = pos.pnl_sol()
                            if self.signal_bot:
                                await self.signal_bot.notify_stop_loss(
                                    pos.symbol, mint, drawdown, pnl
                                )
                            await self.execute_sell(mint, 1.0, reason="sl")
                            continue

                    # Take profit check
                    for mult, frac in list(pos.tp_levels):
                        if pos.entry_price_sol > 0 and price >= pos.entry_price_sol * mult:
                            level_pct = (mult - 1) * 100
                            sell_amount = pos.amount_sol * frac
                            pnl = pos.pnl_sol() * frac
                            logger.info(f"TP +{level_pct:.0f}% triggered for {pos.symbol}")
                            if self.signal_bot:
                                await self.signal_bot.notify_take_profit(
                                    pos.symbol, mint, level_pct, frac, pnl
                                )
                            await self.execute_sell(mint, frac, reason="tp")
                            pos.tp_levels.remove((mult, frac))
                            break

                except Exception as e:
                    logger.error(f"Monitor error for {mint}: {e}")

            await asyncio.sleep(5)

    async def emergency_kill(self):
        logger.warning("☠️ KILL SWITCH: Selling all positions")
        for mint in list(self.positions.keys()):
            await self.execute_sell(mint, 1.0, reason="kill")
