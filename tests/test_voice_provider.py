"""Выбор расшифровки голоса: local | groq | off. Сеть и faster-whisper не нужны: ответ Groq и модель подменены."""
import io
import json
import sys

import pytest

from helpers import OWNER
from kassa_api import bot as kbot
from test_bot import FakeTG, msg, world, q  # noqa: F401


@pytest.mark.parametrize("env,expected", [
    ({}, "local"),
    ({"KASSA_VOICE_ENABLED": "1"}, "local"),
    ({"KASSA_VOICE_ENABLED": "0"}, "off"),                                  # обратная совместимость
    ({"KASSA_VOICE_PROVIDER": "groq"}, "groq"),
    ({"KASSA_VOICE_PROVIDER": " Local "}, "local"),
    ({"KASSA_VOICE_PROVIDER": "off"}, "off"),
    ({"KASSA_VOICE_PROVIDER": "", "KASSA_VOICE_ENABLED": "0"}, "off"),
    ({"KASSA_VOICE_PROVIDER": "groq", "KASSA_VOICE_ENABLED": "0"}, "groq"),  # явный провайдер главнее
    ({"KASSA_VOICE_PROVIDER": "openai"}, "off"),                            # незнакомое: безопаснее выключить
])
def test_voice_provider_from_env(env, expected):
    assert kbot.voice_provider(env) == expected


def test_make_transcriber_by_provider():
    assert kbot.make_transcriber({"KASSA_VOICE_PROVIDER": "off"}) == ("off", None)
    prov, t = kbot.make_transcriber({"KASSA_WHISPER_MODEL": "tiny"})
    assert prov == "local" and isinstance(t, kbot.WhisperTranscriber) and t.model_name == "tiny"
    prov, t = kbot.make_transcriber({"KASSA_VOICE_PROVIDER": "groq", "KASSA_GROQ_API_KEY": "k"})
    assert prov == "groq" and t.model == kbot.GROQ_DEFAULT_MODEL
    _, t = kbot.make_transcriber({"KASSA_VOICE_PROVIDER": "groq", "KASSA_GROQ_API_KEY": "k", "KASSA_GROQ_MODEL": "whisper-large-v3"})
    assert t.model == "whisper-large-v3"


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_groq_request_shape_and_text(tmp_path):
    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"OggS-voice")
    seen = {}

    def opener(req, timeout=None):
        seen["url"], seen["headers"], seen["body"], seen["timeout"] = req.full_url, dict(req.header_items()), req.data, timeout
        return FakeResp(json.dumps({"text": " кофе 150 лир "}).encode())

    t = kbot.GroqTranscriber("SECRETKEY", opener=opener)
    assert t(str(audio)) == "кофе 150 лир"
    assert seen["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert seen["headers"]["Authorization"] == "Bearer SECRETKEY"
    ctype = seen["headers"]["Content-type"]
    boundary = ctype.split("boundary=")[1]
    body = seen["body"]
    assert b"OggS-voice" in body and body.rstrip().endswith(("--%s--" % boundary).encode())
    assert b'name="model"\r\n\r\nwhisper-large-v3-turbo' in body and b'name="file"; filename="voice.ogg"' in body


def test_groq_without_key_says_so(tmp_path):
    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"x")
    with pytest.raises(kbot.VoiceUnavailable):
        kbot.GroqTranscriber("")(str(audio))


def test_groq_http_error_becomes_stt_fail_without_key_in_logs(world, capsys):
    b, tg, dbp, _ = world

    def opener(req, timeout=None):
        raise OSError("network down")
    b.transcribe = kbot.GroqTranscriber("SECRETKEY", opener=opener)
    b.handle_update(msg(voice={"file_id": "f1", "duration": 4}))
    assert tg.sent()[0]["text"] == kbot.STT_FAIL_TEXT and q(dbp, "SELECT COUNT(*) FROM cards") == [(0,)]
    out = capsys.readouterr()
    assert "SECRETKEY" not in out.out + out.err


def test_groq_voice_becomes_a_card(world):
    b, tg, dbp, _ = world
    b.transcribe = kbot.GroqTranscriber("k", opener=lambda req, timeout=None: FakeResp(b'{"text": "\\u043a\\u043e\\u0444\\u0435 150 \\u043b\\u0438\\u0440"}'))
    b.handle_update(msg(voice={"file_id": "f1", "duration": 4}))
    assert q(dbp, "SELECT source, heard FROM cards") == [("voice", "кофе 150 лир")]


def test_missing_groq_key_message_goes_to_user(world):
    b, tg, dbp, _ = world
    b.transcribe = kbot.GroqTranscriber("")
    b.handle_update(msg(voice={"file_id": "f1", "duration": 4}))
    assert tg.sent()[0]["text"] == kbot.VOICE_NO_KEY_TEXT and q(dbp, "SELECT COUNT(*) FROM cards") == [(0,)]


def test_local_without_faster_whisper_gives_clear_message(world, monkeypatch):
    b, tg, dbp, _ = world
    monkeypatch.setitem(sys.modules, "faster_whisper", None)          # импорт падает с ImportError
    b.transcribe = kbot.WhisperTranscriber("tiny", "/nonexistent")
    b.handle_update(msg(voice={"file_id": "f1", "duration": 4}))
    assert tg.sent()[0]["text"] == kbot.VOICE_NO_WHISPER_TEXT and q(dbp, "SELECT COUNT(*) FROM cards") == [(0,)]


def test_off_provider_answers_voice_off(world):
    b, tg, _, heard = world
    b.transcribe, b.voice_enabled = None, False
    b.handle_update(msg(voice={"file_id": "f1", "duration": 4}))
    assert tg.sent()[0]["text"] == kbot.VOICE_OFF_TEXT and not tg.sent("getFile")
