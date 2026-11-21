"""Способы оплаты (карты, банки, биржи, кошельки): данные базы, а не код.

Список живёт в таблице methods (том ./data, файл kassa.db): в новой базе один способ «Наличные», остальные человек
называет при установке. Команда внутри контейнера:

  docker compose exec api python -m kassa_api.methods list
  docker compose exec api python -m kassa_api.methods add "Карта Revolut" --type card
  docker compose exec api python -m kassa_api.methods remove "Карта Revolut"

--type: cash | card | bank | exchange | wallet | other (по умолчанию other). Удаление убирает способ из формы и из
разбора текста, но записи, где он использован, остаются как есть: в отчётах он виден по названию.
"""
import argparse
import os
import sys

from . import db
from .records import RESERVED_METHOD_TITLES, normalize_title as norm

MAX_TITLE = 30


class MethodError(Exception):
    pass


def method_code(title: str) -> str:
    return "x:" + norm(title)


def clean_title(title) -> str:
    text = " ".join(str(title or "").split())
    if not text or len(text) > MAX_TITLE:
        raise MethodError("Название: от 1 до %d знаков." % MAX_TITLE)
    if norm(text) in RESERVED_METHOD_TITLES:
        raise MethodError("Название «%s» занято формой (пункт «Другой…»)." % text)
    return text


def list_methods(conn, include_removed: bool = False) -> list[dict]:
    sql = "SELECT code, title, scope, kind, active FROM methods"
    if not include_removed:
        sql += " WHERE active=1"
    return [dict(r) for r in conn.execute(sql + " ORDER BY sort, title")]


def _find(conn, title: str):
    n = norm(title)
    for r in conn.execute("SELECT code, title, active FROM methods ORDER BY active DESC, sort"):
        if norm(r["title"]) == n:
            return r
    return None


def _audit(conn, action: str, code: str) -> None:
    conn.execute("INSERT INTO audit(ts, actor_id, action, entity, entity_id, via) VALUES (?,?,?,?,?,?)",
                 (db.now_utc_iso(), "cli", action, "method", code, "cli"))


def add_method(conn, title, kind: str = "other") -> tuple[str, str]:
    """-> (код, 'added' | 'restored' | 'exists'). Название ищется без учёта регистра."""
    title = clean_title(title)
    if kind not in db.METHOD_KINDS:
        raise MethodError("Тип: один из %s." % ", ".join(db.METHOD_KINDS))
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _find(conn, title)
        if row is not None and row["active"]:
            result = (row["code"], "exists")
        elif row is not None:
            conn.execute("UPDATE methods SET active=1, scope='both', kind=? WHERE code=?", (kind, row["code"]))
            _audit(conn, "method.restore", row["code"])
            result = (row["code"], "restored")
        else:
            nxt = conn.execute("SELECT COALESCE(MAX(sort),0)+10 FROM methods").fetchone()[0]
            code = method_code(title)
            conn.execute("INSERT INTO methods(code, title, sort, active, scope, kind) VALUES (?,?,?,1,'both',?)",
                         (code, title, nxt, kind))
            _audit(conn, "method.add", code)
            result = (code, "added")
        conn.execute("COMMIT")
        return result
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def remove_method(conn, title) -> str:
    """Выключает способ (строка остаётся: на неё ссылаются записи). -> код."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = _find(conn, " ".join(str(title or "").split()))
        if row is None or not row["active"]:
            raise MethodError("Способа «%s» нет в списке." % title)
        conn.execute("UPDATE methods SET active=0 WHERE code=?", (row["code"],))
        _audit(conn, "method.remove", row["code"])
        conn.execute("COMMIT")
        return row["code"]
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def main(argv=None, env=os.environ) -> int:
    ap = argparse.ArgumentParser(prog="python -m kassa_api.methods", description="Способы оплаты")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="показать способы")
    p_add = sub.add_parser("add", help="добавить способ")
    p_add.add_argument("title")
    p_add.add_argument("--type", dest="kind", default="other", choices=db.METHOD_KINDS)
    p_rm = sub.add_parser("remove", help="убрать способ (записи остаются)")
    p_rm.add_argument("title")
    args = ap.parse_args(argv)
    path = env.get("KASSA_DB_PATH")
    if not path:
        print("KASSA_DB_PATH не задан: запускайте команду внутри контейнера api.", file=sys.stderr)
        return 2
    db.init_db(path, env)
    conn = db.connect(path)
    try:
        if args.cmd == "list":
            for m in list_methods(conn):
                print("%s\t%s" % (m["title"], m["kind"]))
            return 0
        if args.cmd == "add":
            code, state = add_method(conn, args.title, args.kind)
            print({"added": "Добавлен", "restored": "Возвращён", "exists": "Уже есть"}[state] + ": " + conn.execute(
                "SELECT title FROM methods WHERE code=?", (code,)).fetchone()[0])
            return 0
        remove_method(conn, args.title)
        print("Убран: %s" % args.title)
        return 0
    except MethodError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
