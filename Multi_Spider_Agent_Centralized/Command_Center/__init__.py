from pathlib import Path
from dotenv import load_dotenv

# Load .env at package import time, before any module reads os.getenv
load_dotenv(Path(__file__).parent / ".env")
