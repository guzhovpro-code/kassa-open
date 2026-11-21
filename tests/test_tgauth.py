import time

import pytest

from kassa_api import tgauth
from helpers import BOT_ID, OWNER, STRANGER, make_init_data, new_key

PRIV, PUB = new_key()
OTHER_PRIV, OTHER_PUB = new_key()


def verify(init, **kw):
    return tgauth.verify_init_data(init, BOT_ID, kw.pop("pub", PUB), **kw)


def reason(init, **kw):
    with pytest.raises(tgauth.AuthError) as e:
        verify(init, **kw)
    return str(e.value)


def test_valid_signature_returns_user():
    info = verify(make_init_data(PRIV))
    assert info["user"]["id"] == OWNER


def test_foreign_signature_rejected():
    # подписано чужим ключом, проверяем нашим
    assert reason(make_init_data(OTHER_PRIV)) == "bad_signature"


def test_production_key_rejects_test_signature():
    # боевой ключ Telegram по умолчанию не принимает подпись нашего тестового ключа
    with pytest.raises(tgauth.AuthError):
        tgauth.verify_init_data(make_init_data(PRIV), BOT_ID)


def test_tampered_user_id_rejected():
    init = make_init_data(PRIV, user_id=STRANGER)
    # подпись от STRANGER, но user подменён на OWNER уже после подписи
    forged = make_init_data(PRIV, user_id=STRANGER, tamper={"user": '{"id":%d}' % OWNER})
    assert reason(forged) == "bad_signature"
    assert verify(init)["user"]["id"] == STRANGER   # а сама по себе подпись настоящая


def test_missing_signature_rejected():
    assert reason(make_init_data(PRIV, drop_signature=True)) == "no_signature"


def test_other_bot_id_rejected():
    assert reason(make_init_data(PRIV, bot_id="999")) == "bad_signature"


def test_expired_rejected():
    old = int(time.time()) - tgauth.MAX_AGE_SECONDS - 60
    assert reason(make_init_data(PRIV, auth_date=old)) == "expired"


def test_fresh_edge_accepted():
    ok = int(time.time()) - tgauth.MAX_AGE_SECONDS + 120
    assert verify(make_init_data(PRIV, auth_date=ok))["auth_date"] == ok


def test_from_future_rejected():
    fut = int(time.time()) + tgauth.MAX_FUTURE_SECONDS + 600
    assert reason(make_init_data(PRIV, auth_date=fut)) == "from_future"


def test_extra_field_is_part_of_signature():
    init = make_init_data(PRIV, extra={"chat_type": "private"})
    assert verify(init)["user"]["id"] == OWNER
    assert reason(init + "&start_param=x") == "bad_signature"


def test_duplicate_field_rejected():
    init = make_init_data(PRIV)
    assert reason(init + "&auth_date=1") == "duplicate_field"


def test_garbage_inputs():
    for bad in ("", "x", "signature=!!!&auth_date=1", "a" * 9000):
        with pytest.raises(tgauth.AuthError):
            verify(bad)


def test_user_without_id_rejected():
    init = make_init_data(PRIV, extra={"user": '{"first_name":"x"}'})
    assert reason(init) == "no_user_id"


def test_allowed_list():
    allowed = frozenset({OWNER})
    assert tgauth.is_allowed({"id": OWNER}, allowed)
    assert not tgauth.is_allowed({"id": STRANGER}, allowed)
    assert not tgauth.is_allowed({"id": OWNER, "is_bot": True}, allowed)
    assert not tgauth.is_allowed({}, allowed)
