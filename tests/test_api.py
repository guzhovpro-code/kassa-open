import http.client
import json
import threading
import uuid

import pytest

from helpers import BOT_ID, OWNER, STRANGER, make_init_data, new_key
from kassa_api import db
from kassa_api.server import Config, make_servers

PRIV, PUB = new_key()
TOKEN = "A" * 8 + "abcdefghijklmnopqrstuvwxyz012345"
INTERNAL = "J" * 40


@pytest.fixture()
def env(tmp_path):
    db_path = str(tmp_path / "kassa-test.db")
    cfg = Config(TOKEN, BOT_ID, frozenset({OWNER}), INTERNAL, PUB, db_path)
    pub, internal = make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0))
    for s in (pub, internal):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield pub.server_address[1], db_path
    for s in (pub, internal):
        s.shutdown()
        s.server_close()


def req(port, method, path, body=None, init=None, headers=None, raw=None):
    h = dict(headers or {})
    if init is not None:
        h["Authorization"] = "tma " + init
    data = raw
    if body is not None:
        data = json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
    if data is not None:
        h["Content-Length"] = str(len(data))
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    out = r.read()
    c.close()
    try:
        parsed = json.loads(out)
    except ValueError:
        parsed = out
    return r.status, parsed


def payload(**kw):
    p = {"client_id": str(uuid.uuid4()), "direction": "out", "amount": "500", "currency": "TRY",
         "category": "кафе", "date": db.today_local(), "method": "cash"}
    p.update(kw)
    return p


def rows(db_path, where="1=1"):
    c = db.connect(db_path, readonly=True)
    try:
        return c.execute("SELECT COUNT(*) FROM transactions WHERE " + where).fetchone()[0]
    finally:
        c.close()


def post(port, body, init=None, **kw):
    return req(port, "POST", f"/{TOKEN}/api/records", body=body, init=init if init is not None else make_init_data(PRIV), **kw)


# --- авторизация -----------------------------------------------------------------------------------------
def test_post_without_signature_401_and_nothing_written(env):
    port, path = env
    assert req(port, "POST", f"/{TOKEN}/api/records", body=payload())[0] == 401
    assert post(port, payload(), init="garbage")[0] == 401
    other, _ = new_key()
    assert post(port, payload(), init=make_init_data(other))[0] == 401
    assert post(port, payload(), init=make_init_data(PRIV, auth_date=1))[0] == 401
    assert rows(path) == 0


def test_post_foreign_user_403_nothing_written(env):
    port, path = env
    status, body = post(port, payload(), init=make_init_data(PRIV, user_id=STRANGER))
    assert status == 403 and body == {"valid": True, "allowed": False}
    assert rows(path) == 0


def test_meta_requires_auth(env):
    port, _ = env
    assert req(port, "GET", f"/{TOKEN}/api/meta")[0] == 401
    assert req(port, "GET", f"/{TOKEN}/api/meta", init=make_init_data(PRIV, user_id=STRANGER))[0] == 403
    status, body = req(port, "GET", f"/{TOKEN}/api/meta", init=make_init_data(PRIV))
    assert status == 200 and body["test"] is True and len(body["methods"]) == len(db.METHODS_SEED)
    assert str(OWNER) not in json.dumps(body)


def test_records_only_under_secret_prefix_and_only_post(env):
    port, path = env
    assert req(port, "POST", "/api/records", body=payload(), init=make_init_data(PRIV))[0] == 404
    # GET api/records - список записей периода (читает, не пишет): без дат 422, не 404
    assert req(port, "GET", f"/{TOKEN}/api/records", init=make_init_data(PRIV))[0] == 422
    assert req(port, "PUT", f"/{TOKEN}/api/records", body=payload(), init=make_init_data(PRIV))[0] == 404
    assert req(port, "DELETE", f"/{TOKEN}/api/records", init=make_init_data(PRIV))[0] == 404
    assert rows(path) == 0


# --- запись ----------------------------------------------------------------------------------------------
def test_expense_roundtrip(env):
    port, path = env
    cid = str(uuid.uuid4())
    status, body = post(port, payload(client_id=cid, amount="1 234,5"))
    assert status == 200 and body["ok"] and body["duplicate_request"] is False
    c = db.connect(path, readonly=True)
    r = c.execute("SELECT * FROM transactions WHERE id=?", (body["id"],)).fetchone()
    assert (r["amount_cents"], r["via"], r["actor_id"], r["idem_key"]) == (123450, "webapp", str(OWNER), "webapp:" + cid)


def test_repeat_request_one_row(env):
    port, path = env
    p = payload()
    s1, b1 = post(port, p)
    s2, b2 = post(port, p)
    assert (s1, s2) == (200, 200) and b1["id"] == b2["id"] and b2["duplicate_request"] is True
    assert rows(path) == 1


def test_income_without_method_422(env):
    port, path = env
    status, body = post(port, payload(direction="in", category="доход", method=None))
    assert (status, body["error"]) == (422, "method_required")
    assert rows(path, "via='webapp' AND direction='in' AND method IS NULL") == 0 and rows(path) == 0


def test_expense_without_method_422(env):
    port, path = env
    p = payload()
    del p["method"]
    status, body = post(port, p)
    assert (status, body["error"]) == (422, "method_required")
    assert rows(path) == 0


def test_confirm_flow_over_http(env):
    port, path = env
    p = payload(amount="250")
    assert post(port, p)[0] == 200
    q = payload(amount="250")
    status, body = post(port, q)
    assert (status, body["error"], body["warnings"]) == (409, "confirm_required", ["possible_duplicate"])
    assert rows(path) == 1
    status, body = post(port, dict(q, confirm=["possible_duplicate"]))
    assert status == 200 and rows(path) == 2


def test_new_method_over_http(env):
    port, path = env
    status, body = post(port, payload(direction="in", category="доход", method=None, new_method="Мой банк", currency="USD"))
    assert status == 200
    c = db.connect(path, readonly=True)
    assert c.execute("SELECT COUNT(*) FROM methods WHERE title='Мой банк'").fetchone()[0] == 1
    s, m = req(port, "GET", f"/{TOKEN}/api/meta", init=make_init_data(PRIV))
    assert any(x["title"] == "Мой банк" for x in m["methods"])


# --- разбор запроса ---------------------------------------------------------------------------------------
def test_bad_content_type_and_json_and_size(env):
    port, path = env
    init = make_init_data(PRIV)
    assert req(port, "POST", f"/{TOKEN}/api/records", raw=b"x", init=init,
               headers={"Content-Type": "text/plain"})[0] == 415
    assert req(port, "POST", f"/{TOKEN}/api/records", raw=b"{not json", init=init,
               headers={"Content-Type": "application/json"})[0] == 400
    assert req(port, "POST", f"/{TOKEN}/api/records", raw=b"x" * 9000, init=init,
               headers={"Content-Type": "application/json"})[0] == 413
    assert req(port, "POST", f"/{TOKEN}/api/records", raw=b"[1]", init=init,
               headers={"Content-Type": "application/json"})[1]["error"] == "bad_payload"
    assert req(port, "POST", f"/{TOKEN}/api/records", raw=b"\xff\xfe", init=init,
               headers={"Content-Type": "application/json"})[0] == 400
    assert rows(path) == 0


def test_server_survives_after_bad_requests(env):
    port, path = env
    req(port, "POST", f"/{TOKEN}/api/records", raw=b"x" * 9000, init=make_init_data(PRIV),
        headers={"Content-Type": "application/json"})
    assert post(port, payload())[0] == 200 and rows(path) == 1


def test_log_has_no_amounts_or_notes(env, capfd):
    port, _ = env
    post(port, payload(amount="777,77", note="секретная заметка", category="тайная категория"))
    post(port, payload(amount="0"))
    err = capfd.readouterr().err
    assert "record ok" in err and "record deny: amount_not_positive" in err
    for needle in ("777", "секретная", "тайная", str(OWNER)):
        assert needle not in err


def test_page_marks_test_and_points_to_test_db(env):
    port, path = env
    assert path.endswith("kassa-test.db")
    status, body = req(port, "GET", f"/{TOKEN}/api/meta", init=make_init_data(PRIV))
    assert body["test"] is True
    html = http.client.HTTPConnection("127.0.0.1", port)
    html.request("GET", f"/{TOKEN}/")
    page = html.getresponse().read().decode()
    assert "badge-test" in page and "ТЕСТ" in page


ENV = {"KASSA_PATH_TOKEN": "p" * 40, "KASSA_BOT_TOKEN": "123456:abc", "KASSA_ALLOWED_IDS": "1, 2",
       "KASSA_INTERNAL_TOKEN": "x" * 40, "KASSA_PUBLIC_URL": "https://kassa.example.org/"}


def test_config_reads_everything_from_env():
    cfg = Config.from_env(dict(ENV, KASSA_DB_PATH="/data/kassa-test.db"))
    assert cfg.db_path == "/data/kassa-test.db"
    assert cfg.bot_id == "123456" and cfg.allowed_ids == frozenset({1, 2}) and cfg.owner_chat_id == "1"
    assert cfg.webapp_url == "https://kassa.example.org/" + "p" * 40 + "/"
    assert Config.from_env(ENV).db_path.endswith("-test.db")


def test_path_token_is_optional():
    cfg = Config.from_env({k: v for k, v in ENV.items() if k != "KASSA_PATH_TOKEN"})
    assert cfg.path_token == "" and cfg.webapp_url == "https://kassa.example.org/"


@pytest.mark.parametrize("key,value", [("KASSA_BOT_TOKEN", ""), ("KASSA_ALLOWED_IDS", "x"), ("KASSA_ALLOWED_IDS", ""),
                                       ("KASSA_INTERNAL_TOKEN", "short"), ("KASSA_PUBLIC_URL", "http://insecure.example.org"),
                                       ("KASSA_PUBLIC_URL", ""), ("KASSA_PATH_TOKEN", "short")])
def test_config_refuses_bad_env(key, value):
    with pytest.raises(SystemExit):
        Config.from_env(dict(ENV, **{key: value}))


def test_server_without_path_token_serves_the_root(tmp_path):
    cfg = Config("", BOT_ID, frozenset({OWNER}), INTERNAL, PUB, str(tmp_path / "kassa-test.db"))
    pub, internal = make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0))
    threading.Thread(target=pub.serve_forever, daemon=True).start()
    try:
        port = pub.server_address[1]
        assert req(port, "GET", "/healthz")[0] == 200
        assert req(port, "GET", "/api/meta")[0] == 401
        assert req(port, "GET", "/nope")[0] == 404
    finally:
        pub.shutdown()
        pub.server_close()
        internal.server_close()


def test_server_refuses_prod_db_name(tmp_path):
    cfg = Config(TOKEN, BOT_ID, frozenset({OWNER}), INTERNAL, PUB, str(tmp_path / "kassa.db"))
    with pytest.raises(SystemExit):
        make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0))
    assert not (tmp_path / "kassa.db").exists()
