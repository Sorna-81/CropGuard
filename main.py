"""CropGuard application entry point. Run with ``python main.py``."""
import uvicorn
from cropguard.main import app

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
