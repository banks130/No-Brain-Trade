"""
market_maker.py  –  NoBrainTrade Market Maker
================================================
Strategies (injected via add_token):
  basic       – tight spread 20 bps, small orders
  aggressive  – wide spread 50 bps, large orders, fast rebalance
  deep        – ultra‑tight 10 bps, massive order walls
"""

import asyncio
import time
from typing import Dict, Optional

import aiohttp

from config import (
    MM_SPREAD_BPS, MM_ORDER_SIZE_SOL,
    MM_MAX_INVENTORY_SOL, MM_REBALANCE_THRESHOLD_SOL,
    MM_UPDATE_INTERVAL_SEC, DRY_RUN,
)
from utils import logger

STRATEGY_DEFAULTS = {
    "basic": {
        "spread_bps": 20,
        "order_size_sol": 0.5,
        "max_inventory_sol": 2.0,
        "rebalance_threshold_sol": 1.0,
        "update_interval_sec": 5,
    },
    "aggressive": {
        "spread_bps": 50,
        "order_size_sol": 2.0,
        "max_inventory_sol": 8.0,
        "rebalance_threshold_sol": 4.0,
        "update_interval_sec": 3,
    },
    "deep": {
        "spread_bps": 10,
        "order_size_sol": 5.0,
        "max_inventory_sol": 20.0,
        "rebalance_threshold_sol": 10.0,
        "update_interval_sec": 2,
    },
}


class MarketMaker:

    def __init__(self):
        self.tokens: Dict[str, dict] = {}
        self.session: Optional[aiohttp.ClientSession] = None
        self.running = False

    def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession()
        return self.session

    async def add_token(self, mint: str, strategy: str = "basic", config: Optional[dict] = None):
        cfg = config or STRATEGY_DEFAULTS.get(strategy, STRATEGY_DEFAULTS["basic"])
        self.tokens[mint] = {
            "inventory_sol": 0.0,
            "strategy": strategy,
            "cfg": cfg,
            "started_at": time.time(),
            "trade_count": 0,
            "volume_sol": 0.0,
        }
        logger.info(f"MM started for {mint} | strategy={strategy} | spread={cfg['spread_bps']}bps")

    async def remove_token(self, mint: str):
        if mint in self.tokens:
            await self._cancel_all_orders(mint)
            del self.tokens[mint]
            logger.info(f"MM stopped for {mint}")

    async def _cancel_all_orders(self, mint: str):
        pass

    async def get_mid_price(self, mint: str) -> Optional[float]:
        try:
            async with self._get_session().get(
                f"https://frontend-api.pump.fun/coins/{mint}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()
                return float(data.get("market_cap", 0))
        except Exception:
            return None

    async def _place_order(self, mint: str, side: str, price: float, size_sol: float):
        if DRY_RUN:
            logger.info(
                f"[DRY MM] {side.upper()} {size_sol:.4f} SOL of {mint[:8]}… @ {price:.6f}"
            )
            entry = self.tokens.get(mint)
            if entry:
                entry["trade_count"] += 1
                entry["volume_sol"] += size_sol
                entry["inventory_sol"] += size_sol if side == "buy" else -size_sol
            return

        action = "buy" if side == "buy" else "sell"
        from config import PRIVATE_KEY
        if not PRIVATE_KEY:
            return
        payload = {
            "action": action,
            "mint": mint,
            "amount": size_sol,
            "denominatedInSol": "true",
            "slippage": 100,
            "priorityFee": 0.001,
            "privateKey": PRIVATE_KEY,
        }
        try:
            async with self._get_session().post(
                "https://pumpportal.fun/api/trade", json=payload,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
                if data.get("error"):
                    logger.warning(f"MM order error ({mint}): {data}")
                    return
            entry = self.tokens.get(mint)
            if entry:
                entry["trade_count"] += 1
                entry["volume_sol"] += size_sol
                entry["inventory_sol"] += size_sol if side == "buy" else -size_sol
        except Exception as e:
            logger.error(f"MM place_order exception: {e}")

    async def refresh_orders(self, mint: str):
        entry = self.tokens.get(mint)
        if not entry:
            return
        cfg = entry["cfg"]
        spread = cfg["spread_bps"] / 10_000
        size = cfg["order_size_sol"]
        max_inv = cfg["max_inventory_sol"]
        rebal_thresh = cfg.get("rebalance_threshold_sol", max_inv * 0.6)

        mid = await self.get_mid_price(mint)
        if not mid or mid <= 0:
            return

        bid_price = mid * (1 - spread / 2)
        ask_price = mid * (1 + spread / 2)
        inventory = entry["inventory_sol"]

        if inventory > rebal_thresh:
            bid_size = size * 0.4
            ask_size = size * 1.6
        elif inventory < -rebal_thresh:
            bid_size = size * 1.6
            ask_size = size * 0.4
        else:
            bid_size = ask_size = size

        if abs(inventory) >= max_inv:
            logger.warning(f"MM {mint[:8]}: inventory limit hit ({inventory:.2f} SOL). Rebalancing.")
            await self._rebalance(mint)
            return

        await self._place_order(mint, "buy", bid_price, bid_size)
        await self._place_order(mint, "sell", ask_price, ask_size)
        logger.debug(
            f"MM {mint[:8]} | mid={mid:.4f} | bid={bid_price:.4f}×{bid_size:.2f} | "
            f"ask={ask_price:.4f}×{ask_size:.2f} | inv={inventory:.2f}"
        )

    async def _rebalance(self, mint: str):
        entry = self.tokens.get(mint)
        if not entry:
            return
        cfg = entry["cfg"]
        rebal_thresh = cfg.get("rebalance_threshold_sol", cfg["max_inventory_sol"] * 0.6)
        inventory = entry["inventory_sol"]
        mid = await self.get_mid_price(mint) or 0

        if inventory > rebal_thresh:
            sell_amount = (inventory - rebal_thresh) / 2
            logger.info(f"MM rebalance SELL {sell_amount:.4f} SOL of {mint[:8]}")
            await self._place_order(mint, "sell", mid * 0.99, sell_amount)
        elif inventory < -rebal_thresh:
            buy_amount = (-inventory - rebal_thresh) / 2
            logger.info(f"MM rebalance BUY {buy_amount:.4f} SOL of {mint[:8]}")
            await self._place_order(mint, "buy", mid * 1.01, buy_amount)

    async def run(self):
        self.running = True
        logger.info("MarketMaker loop started.")
        while self.running:
            for mint in list(self.tokens.keys()):
                await self.refresh_orders(mint)
            await asyncio.sleep(MM_UPDATE_INTERVAL_SEC)

    async def emergency_kill(self):
        self.running = False
        self.tokens.clear()
        logger.info("MM emergency kill: all sessions cleared.")

    def get_status(self) -> str:
        if not self.tokens:
            return "📊 No active MM sessions."
        lines = ["📊 <b>Market Maker Status</b>\n"]
        for mint, d in self.tokens.items():
            elapsed = int((time.time() - d.get("started_at", time.time())) / 60)
            lines.append(
                f"• <code>{mint[:10]}…</code> | {d['strategy']} | "
                f"Inv: {d['inventory_sol']:.2f} SOL | "
                f"Trades: {d['trade_count']} | "
                f"Vol: {d['volume_sol']:.2f} SOL | "
                f"Up: {elapsed}m"
            )
        return "\n".join(lines)
