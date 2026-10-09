import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env if present
env_path = Path(__file__).resolve().parent.parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

# GitHub Integration
GITHUB_USERNAME: str = os.getenv("GITHUB_USERNAME", "MrigankKumawat")
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")

# Database Configuration
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite:///./nudge.db")

# Scan & Hunter Settings
MAX_SEARCH_RESULTS: int = int(os.getenv("MAX_SEARCH_RESULTS", "1000"))
DEFAULT_PAGE_SIZE: int = int(os.getenv("DEFAULT_PAGE_SIZE", "100"))