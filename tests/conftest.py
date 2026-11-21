import os
import sys
from pathlib import Path

# Тесты рассчитаны на фиксированный часовой пояс UTC+3 (Europe/Istanbul): задаём его до импорта kassa_api.
os.environ.setdefault("KASSA_TZ", "Europe/Istanbul")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "app"))
sys.path.insert(0, str(HERE))
