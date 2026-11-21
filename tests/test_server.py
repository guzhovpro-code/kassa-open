import http.client
import json
import threading

import pytest

from kassa_api.server import Config, make_servers
from helpers import BOT_ID, OWNER, STRANGER, make_init_data, new_key

PRIV, PUB = new_key()
TOKEN = "T" * 8 + "abcdefghijklmnopqrstuvwxyz012345"      # 40 символов
INTERNAL = "I" * 40


@pytest.fixture(scope="module")
def ports(tmp_path_factory):
    db_path = str(tmp_path_factory.mktemp("srv") / "kassa-test.db")
    cfg = Config(TOKEN, BOT_ID, frozenset({OWNER}), INTERNAL, PUB, db_path)
    pub, internal = make_servers(cfg, ("127.0.0.1", 0), ("127.0.0.1", 0))
    for s in (pub, internal):
        threading.Thread(target=s.serve_forever, daemon=True).start()
    yield pub.server_address[1], internal.server_address[1]
    for s in (pub, internal):
        s.shutdown()
        s.server_close()


def call(port, path, method="GET", headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, headers=headers or {})
    r = c.getresponse()
    body = r.read()
    out = (r.status, dict((k.lower(), v) for k, v in r.getheaders()), body)
    c.close()
    return out


def who(port, init):
    return call(port, f"/{TOKEN}/api/whoami", headers={"Authorization": "tma " + init})


def test_page_on_secret_path(ports):
    s, h, b = call(ports[0], f"/{TOKEN}/")
    assert s == 200 and "Касса".encode() in b
    assert h["x-robots-tag"].startswith("noindex")
    assert "frame-ancestors https://web.telegram.org" in h["content-security-policy"]
    assert h["content-type"].startswith("text/html")


def test_static_assets(ports):
    for name in ("app.js", "app.css"):
        assert call(ports[0], f"/{TOKEN}/{name}")[0] == 200


def test_healthz_on_secret_path(ports):
    assert call(ports[0], f"/{TOKEN}/healthz")[0] == 200


@pytest.mark.parametrize("path", [
    "/", "/healthz", "/api/whoami", "/index.html", "/robots.txt", "/app.js", "/x/",
    "/" + TOKEN[:-1] + "/",                      # токен на символ короче
    "/" + TOKEN,                                 # без хвостового слэша
    "/" + TOKEN.swapcase() + "/",                # другой регистр
])
def test_everything_else_is_404(ports, path):
    s, h, b = call(ports[0], path)
    assert s == 404 and b == b"404 page not found\n"
    assert h["x-robots-tag"].startswith("noindex")


def test_unknown_under_secret_and_other_methods_404(ports):
    assert call(ports[0], f"/{TOKEN}/nope")[0] == 404
    assert call(ports[0], f"/{TOKEN}/.env")[0] == 404
    assert call(ports[0], f"/{TOKEN}/../etc/passwd")[0] == 404
    assert call(ports[0], f"/{TOKEN}/", method="POST")[0] == 404
    assert call(ports[0], "/", method="DELETE")[0] == 404


def test_whoami_valid_allowed(ports):
    s, _, b = who(ports[0], make_init_data(PRIV))
    assert s == 200 and json.loads(b) == {"valid": True, "allowed": True}


def test_whoami_without_header_401(ports):
    s, _, b = call(ports[0], f"/{TOKEN}/api/whoami")
    assert s == 401 and json.loads(b) == {"valid": False, "allowed": False}


def test_whoami_foreign_signature_401(ports):
    other, _ = new_key()
    s, _, b = who(ports[0], make_init_data(other))
    assert s == 401 and json.loads(b) == {"valid": False, "allowed": False}


def test_whoami_expired_401(ports):
    s, _, _ = who(ports[0], make_init_data(PRIV, auth_date=1))
    assert s == 401


def test_whoami_foreign_user_id_403(ports):
    s, _, b = who(ports[0], make_init_data(PRIV, user_id=STRANGER))
    assert s == 403 and json.loads(b) == {"valid": True, "allowed": False}


def test_whoami_bot_user_403(ports):
    s, _, _ = who(ports[0], make_init_data(PRIV, is_bot=True))
    assert s == 403


def test_whoami_response_has_no_ids(ports):
    _, _, b = who(ports[0], make_init_data(PRIV))
    assert set(json.loads(b)) == {"valid", "allowed"} and str(OWNER).encode() not in b


def test_internal_ping(ports):
    assert call(ports[1], "/internal/ping")[0] == 401
    assert call(ports[1], "/internal/ping", headers={"Authorization": "Bearer wrong"})[0] == 401
    assert call(ports[1], "/internal/ping", headers={"Authorization": "Bearer " + INTERNAL})[0] == 200
    assert call(ports[1], "/")[0] == 404
    # секретный префикс публичного порта на внутреннем не работает
    assert call(ports[1], f"/{TOKEN}/")[0] == 404


def test_public_port_has_no_internal_routes(ports):
    assert call(ports[0], "/internal/ping", headers={"Authorization": "Bearer " + INTERNAL})[0] == 404
    assert call(ports[0], f"/{TOKEN}/internal/ping")[0] == 404


def test_config_rejects_short_path_token():
    with pytest.raises(SystemExit):
        Config.from_env({"KASSA_PATH_TOKEN": "short", "KASSA_BOT_ID": "1",
                         "KASSA_ALLOWED_USER_IDS": "1", "KASSA_INTERNAL_TOKEN": "x" * 40})
