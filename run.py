import threading
import webbrowser
import os

import uvicorn

from app import config

if __name__ == "__main__":
    if not os.getenv("K_SERVICE") and config.CLOUD_BACKEND == "local":
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{config.PORT}")).start()
    uvicorn.run("app.main:app", host="0.0.0.0", port=config.PORT)
