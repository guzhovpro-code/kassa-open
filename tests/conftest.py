import os
import sys
from pathlib import Path

# Тесты рассчитаны на фиксированный часовой пояс UTC+3 (Europe/Istanbul): задаём его до импорта kassa_api.
os.environ.setdefault("KASSA_TZ", "Europe/Istanbul")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "app"))
sys.path.insert(0, str(HERE))

# Способы оплаты - данные базы, в коде их нет (в новой базе только «Наличные»). Выдуманный набор для тестов: подкладывается
# в начальное состояние каждой новой базы. Тесты самих способов (test_methods.py) берут настоящее начальное состояние.
from kassa_api import db  # noqa: E402

REAL_METHODS_SEED = list(db.METHODS_SEED)
from helpers import TEST_METHODS_SEED  # noqa: E402

db.METHODS_SEED = list(TEST_METHODS_SEED)
