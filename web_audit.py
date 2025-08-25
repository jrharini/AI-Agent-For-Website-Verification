import subprocess
import threading
import time
import os

# Step 1: Start your Flask app
def run_flask():
    print("🚀 Starting Flask app...")
    subprocess.run(["python", "app2.py"])

# Step 2: Run Lighthouse after Flask starts
def run_lighthouse():
    time.sleep(5)  # Wait for Flask to start
    print("🔍 Running Lighthouse audit...")
    subprocess.run([
    "C:/Users/iamjr/AppData/Roaming/npm/lighthouse.cmd",
    "http://127.0.0.1:5000",
    "--output", "html",
    "--output-path", "audit-report.html",
    "--chrome-flags=--headless"
])

    print("✅ Audit complete. Report saved as audit-report.html")

# Run both in parallel
flask_thread = threading.Thread(target=run_flask)
lighthouse_thread = threading.Thread(target=run_lighthouse)

flask_thread.start()
lighthouse_thread.start()

flask_thread.join()
lighthouse_thread.join()
