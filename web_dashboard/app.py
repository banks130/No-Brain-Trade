from flask import Flask, jsonify, render_template
from flask_cors import CORS
from waitress import serve
import os
import sys

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

def start_flask(host="0.0.0.0", port=None):
    if port is None:
        port = int(os.environ.get("PORT", 8080))
    print(f"✅ Binding Waitress to {host}:{port}", flush=True)
    sys.stdout.flush()
    serve(app, host=host, port=port)
