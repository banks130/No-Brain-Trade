from flask import Flask, jsonify, render_template
from flask_cors import CORS
from waitress import serve
import os
import sys
import time

app = Flask(__name__, template_folder=".")
CORS(app)

detector = None
market_maker = None

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/health")
def health():
    return "OK", 200

@app.route("/api/tokens")
def api_tokens():
    if detector is None:
        return jsonify([])
    try:
        tokens = detector.get_spiked_tokens()
        result = []
        for t in sorted(tokens, key=lambda x: x.spike_pct, reverse=True):
            result.append({
                "mint": t.mint,
                "name": t.name,
                "symbol": t.symbol,
                "mcap": round(t.current_mcap, 4),
                "initial_mcap": round(t.initial_mcap, 4),
                "peak_mcap": round(t.peak_mcap, 4),
                "spike_pct": round(t.spike_pct, 1),
                "wallets": t.unique_wallet_count,
                "buy_ratio": round(t.buy_ratio, 3),
                "net_sol": round(t.net_sol_flow, 4),
                "age_sec": round(t.age_seconds, 0),
                "buy_count": t.buy_count,
                "sell_count": t.sell_count,
            })
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/active")
def api_active():
    if detector is None:
        return jsonify({"active": 0, "spiked": 0})
    try:
        active = len(detector.get_active_tokens())
        spiked = len(detector.get_spiked_tokens())
        return jsonify({"active": active, "spiked": spiked})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/mm_status")
def api_mm_status():
    if market_maker is None:
        return jsonify({"status": "disabled", "tokens": []})
    try:
        positions = {}
        if hasattr(market_maker, '_positions'):
            for mint, pos in market_maker._positions.items():
                positions[mint] = {
                    "mint": mint,
                    "inventory": round(getattr(pos, 'inventory', 0), 4),
                    "pnl": round(getattr(pos, 'pnl', 0), 4),
                }
        return jsonify({
            "status": "running",
            "tokens": list(positions.values()),
            "token_count": len(positions),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stats")
def api_stats():
    if detector is None:
        return jsonify({"tracked": 0, "spiked": 0, "active": 0})
    try:
        all_tokens = list(detector._tokens.values()) if hasattr(detector, '_tokens') else []
        active = detector.get_active_tokens()
        spiked = detector.get_spiked_tokens()
        return jsonify({
            "tracked": len(all_tokens),
            "active": len(active),
            "spiked": len(spiked),
            "uptime_sec": round(time.time(), 0),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def start_flask(host="0.0.0.0", port=None):
    if port is None:
        port = int(os.environ.get("PORT", 8080))
    print(f"✅ Binding Waitress to {host}:{port}", flush=True)
    sys.stdout.flush()
    serve(app, host=host, port=port)
