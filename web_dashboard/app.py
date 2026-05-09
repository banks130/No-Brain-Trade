from flask import Flask, jsonify, render_template
from flask_cors import CORS
import asyncio
import threading

app = Flask(__name__, template_folder=".")
CORS(app)

# These will be set from main.py after detector and mm are created
detector = None
market_maker = None

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/tokens")
def api_tokens():
    if not detector:
        return jsonify([])
    spiked = detector.get_spiked_tokens()
    return jsonify([{
        "mint": t.mint,
        "name": t.name,
        "symbol": t.symbol,
        "mcap": t.current_mcap,
        "spike_pct": t.spike_pct,
        "peak_mcap": t.peak_mcap,
        "wallets": t.unique_wallet_count,
        "buy_ratio": t.buy_ratio,
        "net_sol": t.net_sol_flow,
        "age": t.age_seconds,
    } for t in spiked])

@app.route("/api/mm_status")
def api_mm_status():
    if market_maker:
        return jsonify({"status": "active", "tokens": list(market_maker.tokens.keys())})
    return jsonify({"status": "inactive"})

def start_flask(host="0.0.0.0", port=5000):
    # Use waitress for production; for development, Flask's built-in works
    app.run(host=host, port=port, debug=False, use_reloader=False)
