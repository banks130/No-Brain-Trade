# 🧠 No Brain Trade

Fully automated crypto trading system for **pump.fun tokens** on Solana.

**⚠️ WARNING**: This is for educational purposes only. Trading memecoins is extremely risky. You assume all responsibility for any funds lost. Always test with `DRY_RUN=true` first.

## Features
- 📊 **Live 150% Spike Terminal** – Real‑time web dashboard showing tokens that have pumped ≥150%.
- 🔔 **Telegram Signal Channel** – Automatic alerts with spike details, plus admin commands.
- 🤖 **Auto‑Trade Bot** – Buys tokens with strong signals, manages take‑profit & stop‑loss.
- 🏦 **Market‑Making Bot** – Maintains bid/ask spread on selected tokens, with inventory controls.
- 🛑 **Emergency Kill Switch** – Instantly sells all positions and stops MM.

## Quick Start
1. `cd NoBrainTrade`
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and fill in your keys.
4. `python main.py`
5. Open `http://localhost:5000` to view the terminal.
