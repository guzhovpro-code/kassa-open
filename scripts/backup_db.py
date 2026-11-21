"""Снимок базы кассы через sqlite backup API (живую базу не меняет).

    python3 scripts/backup_db.py [источник] [назначение]

По умолчанию: ./data/kassa.db -> ./backups/kassa-<UTC-время>.db (права 600). Источник открывается только на чтение.
Печатает имя снимка, число строк в transactions и результат integrity_check. Существующий снимок не перезаписывается.
Восстановление: остановить сервисы (docker compose down), положить снимок на место data/kassa.db, запустить заново.
"""
import os
import sqlite3
import sys
from datetime import datetime, timezone


def backup(src_path: str, dst_path: str) -> str:
    if os.path.exists(dst_path):
        raise SystemExit("снимок %s уже есть: не перезаписываю" % dst_path)
    os.makedirs(os.path.dirname(os.path.abspath(dst_path)), exist_ok=True)
    tmp_path = dst_path + ".tmp"
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    src = sqlite3.connect("file:%s?mode=ro" % os.path.abspath(src_path), uri=True, timeout=30)
    dst = sqlite3.connect(tmp_path)
    try:
        with dst:
            src.backup(dst)
        rows = dst.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        dst.execute("PRAGMA journal_mode=DELETE")
    finally:
        dst.close()
        src.close()
    if check != "ok":
        os.remove(tmp_path)
        raise SystemExit("integrity_check: %s" % check)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, dst_path)
    return "backup %s rows=%d integrity=%s" % (os.path.basename(dst_path), rows, check)


if __name__ == "__main__":
    source = sys.argv[1] if len(sys.argv) > 1 else "data/kassa.db"
    target = sys.argv[2] if len(sys.argv) > 2 else "backups/kassa-%s.db" % \
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(backup(source, target))
