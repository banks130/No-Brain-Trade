#!/usr/bin/env python3
import threading
import time
import os
from web_dashboard.app import start_flask

flask_port = int(os.environ.get("PORT", 8080))
flask_thread = threading.Thread(
    target=start_flask,
    args=("0.0.0.0", flask_port),
    daemon=True
)
flask_thread.start()
time.sleep(2)  # ensure Waitress binds before Railway checks

print(f"Flask running on port {flask_port}")

# Keep the main thread alive (Railway will kill the container if it exits)
while True:
    time.sleep(60)
