import importlib.util
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from kassa_api import db

spec = importlib.util.spec_from_file_location("backup_db", Path(__file__).resolve().parent.parent / "scripts" / "backup_db.py")
backup_db = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_db)


def test_backup_is_a_private_valid_copy_and_keeps_the_source(tmp_path):
    src = str(tmp_path / "kassa-test.db")
    db.init_db(src)
    before = Path(src).read_bytes()
    out = tmp_path / "backups" / "copy.db"
    line = backup_db.backup(src, str(out))
    assert "integrity=ok" in line and "rows=0" in line
    assert stat.S_IMODE(os.stat(out).st_mode) == 0o600
    c = sqlite3.connect(out)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    c.close()
    assert Path(src).read_bytes() == before


def test_backup_never_overwrites(tmp_path):
    src = str(tmp_path / "kassa-test.db")
    db.init_db(src)
    out = str(tmp_path / "copy.db")
    backup_db.backup(src, out)
    with pytest.raises(SystemExit):
        backup_db.backup(src, out)
