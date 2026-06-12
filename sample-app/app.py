import os
from flask import Flask

app = Flask(__name__)

# Deliberate bug for the demo: this env var is never set in the first
# deployment manifest, so the app crashes on startup -> CrashLoopBackOff.
APP_MODE = os.environ["APP_MODE"]


@app.route("/")
def index():
    return f"Hello from sample-app! mode={APP_MODE}\n"


@app.route("/healthz")
def healthz():
    return "ok\n"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
