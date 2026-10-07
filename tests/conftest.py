import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Tests never call the real API.
os.environ["ANTHROPIC_API_KEY"] = ""
