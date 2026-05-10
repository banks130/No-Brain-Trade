from flask import Flask, jsonify
from flask_cors import CORS
from waitress import serve
import os

app = Flask(__name__)
CORS(app)

@app.route("/")
def index():
    return "NoBrainTrade Dashboard is LIVE. Add your HTML later."

@app.route("/health")
def health():
    return "OK", 200

def start_flask(host="0.0.0.0", port=None):
    if port is None:
        port = int(os.environ.get("PORT", 8080))
    serve(app, host=host, port=port)
