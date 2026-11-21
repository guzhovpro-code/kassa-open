"""Обмен валют в свободном тексте: «поменял 201 230 рублей на 75 000 лир и 700 долларов, комиссия 758,65».

Отдельный модуль на стороне кассы: moneytext.py не меняется. Из moneytext берутся числа, валюты, способы и даты (candidates, METHOD_RULES, date_of). Только стандартная
библиотека и словари, без LLM.

parse(text, today) -> dict | None:
- None: это не обмен, дальше работает обычный разбор денег (moneytext.parse);
- dict: черновик обмена в форме запроса POST /api/exchanges (date, give, get, fee, note) плюс kind='exchange';
- ExchangeReject: сообщение целиком по форме обмен, но суммы или валюты не сходятся (сумма без валюты, одна валюта с двух
  сторон, валюта названа без суммы, доля на несуществующую валюту) - карточки нет, ничего не записано.

Подход (третье исправление ложных срабатываний подряд, решение «менять подход, а не фразу»): СТРОГАЯ ГРАММАТИКА ВСЕГО
СООБЩЕНИЯ, а не поиск обмена внутри текста. Распознанные куски (глагол, суммы с валютой, комиссия, доля, способ, дата)
заменяются метками, и то, что осталось, обязано целиком совпасть с одной из форм (начало и конец привязаны):

    [дата] ГЛАГОЛ [способ] СУММА на|в СУММА [(и|,) СУММА]{0,2}  [, способ | дата | комиссия | из них N на валюту]*
    [дата] купил  СУММА [(и|,) СУММА]{0,2} за СУММА                                [те же хвосты]
    [дата] продал СУММА за СУММА [(и|,) СУММА]{0,2}                                [те же хвосты]

ГЛАГОЛ: поменял, обменял, сменял, конвертировал, разменял и их формы («перевёл» исключён: перевод неоднозначен).
Купили/продали должна быть сама валюта (сумма с валютой). Любой остаток - второй глагол («и оплатил ...»), «на карту»,
товар, второе предложение, лишнее слово - значит НЕ обмен, и сообщение идёт обычным разбором денег. Неуверенность всегда в
сторону «не обмен»: трата не теряется. Исключение одно: форма совпала целиком, но суммы не сходятся (см. выше) - отказ
с просьбой ввести обмен формой, потому что ни обмен, ни трата из такой фразы не получаются.

Страховка механизма - tests/test_exchange_corpus.py: все денежные фразы существующих тестов и 30+ ловушек с двумя валютами
не должны стать обменом, плюс список положительных фраз с точным разбором. Новое ложное срабатывание - одна строка туда.
"""
import re
from decimal import Decimal

from . import moneytext
from .moneytext import Reject

MAX_LEGS = 3
REJECT_CODE = "unsure_exchange"


class ExchangeReject(Exception):
    """Сообщение по форме обмен, но однозначного разбора нет: карточки не будет."""

    def __init__(self, code: str = REJECT_CODE):
        super().__init__(code)
        self.code = code


_VERB = re.compile(r"(?<![а-я])(?:поменял\w*|обменял\w*|сменял\w*|конвертиров\w*|конвертнул\w*|разменял\w*"
                   r"|купил\w*|продал\w*)(?![а-я])")
_ZERO_FEE = re.compile(r"(?<![а-я])(?:без\s+комисси\w+|комисси\w+\s+нет|комисси\w+\s+нулев\w+)(?![а-я])")
_FEE_WORD = re.compile(r"(?<![а-я])комисси\w*")
_FEE_GAP = re.compile(r"^[\s:=\-–—]*(?:(?:банка|банку|обмена|обменника|обменнику|составила|составил|составило|была|"
                      r"было|вышла|вышло|около|примерно|за\s+обмен)[\s:=\-–—]*)*$")
_PART_WORD = re.compile(r"(?<![а-я])из\s+(?:них|которых|этих)(?![а-я])")
_PART_TARGET = re.compile(r"\s*(?:на|за|в|под)\s+([^\W\d_]+)")
_DATE = re.compile(r"(?<![а-я])(?:позавчера|вчера|сегодня)(?![а-я])")
_WORD = re.compile(r"[^\W\d_]+")
_WORD_TAIL = re.compile(r"[^\W_]*")
_METHOD_PREFIX = re.compile(r"(?<![а-я])(с|со|из|на|в)\s+$")
_SYMBOL_BEFORE = re.compile(r"[$€£₽₺]\s?$")

_TAIL = r"(?:\s*,?\s*(?:§M|§D|§F|§Z|§P))*"
_AMT = r"§[ANW]"
_GETS = _AMT + r"(?:\s*(?:,|и)\s*" + _AMT + r"){0,2}"
_GIVE_FIRST = re.compile(r"^(?:§D\s+)?§V(?:\s+§M)*\s+(?P<give>" + _AMT + r")\s+(?P<mark>на|в|во)\s+(?P<gets>" + _GETS
                         + r")" + _TAIL + r"$")
_SELL = re.compile(r"^(?:§D\s+)?§V(?:\s+§M)*\s+(?P<give>" + _AMT + r")\s+(?P<mark>за)\s+(?P<gets>" + _GETS + r")"
                   + _TAIL + r"$")
_BUY = re.compile(r"^(?:§D\s+)?§V(?:\s+§M)*\s+(?P<gets>" + _GETS + r")\s+за\s+(?P<give>" + _AMT + r")" + _TAIL + r"$")

# падежи, которых нет в moneytext.CURRENCY_RULES («из них 150 000 на лиры», «поменял доллары на ...»)
_CUR_PREFIX = (("рубл", "RUB"), ("лир", "TRY"), ("доллар", "USD"), ("бакс", "USD"), ("евро", "EUR"), ("юан", "CNY"),
               ("фунт", "GBP"), ("тенге", "KZT"), ("дирхам", "AED"), ("гривн", "UAH"))


def _cur_word(token: str):
    """Код валюты по слову («лиры» -> TRY) или None."""
    if not token:
        return None
    for code, rx in moneytext.CURRENCY_RULES:
        if rx.fullmatch(token):
            return code
    for prefix, code in _CUR_PREFIX:
        if token.startswith(prefix):
            return code
    return None


def _amount(value) -> str:
    """Decimal -> строка суммы; не больше двух знаков и в пределах потолка, иначе отказ."""
    if value is None or value <= 0 or value > moneytext.MAX_UNITS:
        raise ExchangeReject()
    try:
        return moneytext._fmt_amount(Decimal(value))
    except Reject:
        raise ExchangeReject() from None


def _span(low: str, c) -> tuple:
    """Участок кандидата вместе с валютой: символ перед числом или слово-валюта сразу после него."""
    start, end = c["start"], c["end"]
    m = _SYMBOL_BEFORE.search(low[:start])
    if m:
        start = m.start()
    m = re.match(r"\s?[^\s\d,.;:!?()]+", low[end:])
    if m and _cur_word(m.group(0).strip()):
        end += m.end()
    return start, end


class _Spans:
    """Непересекающиеся распознанные участки: (начало, конец, метка, данные)."""

    def __init__(self, n: int):
        self.busy = [False] * n
        self.items = []

    def free(self, s: int, e: int) -> bool:
        return not any(self.busy[s:e])

    def add(self, s: int, e: int, tok: str, data=None) -> bool:
        if not self.free(s, e):
            return False
        for i in range(s, e):
            self.busy[i] = True
        self.items.append((s, e, tok, data))
        return True


def _skeleton(low: str):
    """Текст -> (скелет с метками §V §A §N §W §F §Z §P §M §D, участки) или None (глаголов не один)."""
    verbs = list(_VERB.finditer(low))
    if len(verbs) != 1:
        return None
    sp = _Spans(len(low))
    v = verbs[0]
    filler = re.compile(r"\s+(?:себе|мне|нам|еще)(?![а-я])").match(low, v.end())    # «купил себе 300 долларов за ...»
    sp.add(v.start(), filler.end() if filler else v.end(), "V", v.group(0))
    for m in _ZERO_FEE.finditer(low):
        sp.add(m.start(), m.end(), "Z")
    cands = [c for c in moneytext.candidates(low) if not c["unit"]]
    used = set()

    fee_m = _FEE_WORD.search(low)
    if fee_m is not None and sp.free(fee_m.start(), fee_m.end()):
        for i, c in enumerate(cands):
            if c["start"] >= fee_m.end():
                if _FEE_GAP.match(low[fee_m.end():c["start"]]) and not c["bad"]:
                    s, e = _span(low, c)
                    if sp.add(fee_m.start(), e, "F", (c["value"], c["cur"])):
                        used.add(i)
                break

    for m in _PART_WORD.finditer(low):
        for i, c in enumerate(cands):
            if i in used or c["start"] < m.end() or c["bad"] or low[m.end():c["start"]].strip():
                continue
            s, e = _span(low, c)
            t = _PART_TARGET.match(low[e:])
            target = _cur_word(t.group(1)) if t else None
            if target is not None and sp.add(m.start(), e + t.end(), "P", (c["value"], c["cur"], target)):
                used.add(i)
            break

    for i, c in enumerate(cands):
        if i in used or c["bad"] or c["half"] or c["kword"]:
            continue
        s, e = _span(low, c)
        sp.add(s, e, "A" if c["cur"] else "N", (c["value"], c["cur"]))

    for code, rx in moneytext.METHOD_RULES:
        for m in rx.finditer(low):
            end = _WORD_TAIL.match(low, m.end()).end()          # правило даёт основу («тинькоф»), берём слово целиком
            if not sp.free(m.start(), end):
                continue
            s, prefix = m.start(), ""
            p = _METHOD_PREFIX.search(low[:s])
            if p and sp.free(p.start(), s):
                s, prefix = p.start(), p.group(1)
            sp.add(s, end, "M", (code, prefix))

    for m in _DATE.finditer(low):
        sp.add(m.start(), m.end(), "D")

    for m in _WORD.finditer(low):                           # валюта словом без суммы: «на лиры»
        if sp.free(m.start(), m.end()) and _cur_word(m.group(0)):
            sp.add(m.start(), m.end(), "W", _cur_word(m.group(0)))

    out, pos = [], 0
    for s, e, tok, _ in sorted(sp.items):
        out.append(low[pos:s])
        out.append(" §%s " % tok)
        pos = e
    out.append(low[pos:])
    text = " ".join("".join(out).split())
    text = re.sub(r"\s+,", ",", text)
    return re.sub(r"[.!;,\s]+$", "", text), sorted(sp.items)


def parse(text, today: str):
    raw = " ".join(str(text or "").split())
    low = moneytext.norm(raw)
    if not low or not _VERB.search(low):
        return None
    sk = _skeleton(low)
    if sk is None:
        return None
    skeleton, items = sk
    verb = next(d for _, _, t, d in items if t == "V")
    kind = "buy" if verb.startswith("купил") else "sell" if verb.startswith("продал") else "swap"
    rx = {"buy": _BUY, "sell": _SELL, "swap": _GIVE_FIRST}[kind]
    m = rx.match(skeleton)
    if m is None:
        return None                                          # форма не та: не обмен, обычный разбор денег

    # метки по порядку в тексте -> роли
    n_amt = sum(1 for _, _, t, _ in items if t in ("A", "N", "W"))
    n_gets = len(re.findall(_AMT, m.group("gets")))
    if n_amt != 1 + n_gets:
        return None
    ordered = [(s, t, d) for s, e, t, d in items if t in ("A", "N", "W")]
    if kind == "buy":
        gets, give = ordered[:-1], ordered[-1]
    else:
        give, gets = ordered[0], ordered[1:]
    if any(t == "N" for _, t, _ in ordered) or any(t == "W" for _, t, _ in ordered):
        if kind != "swap" and not any(t == "W" for _, t, _ in ordered):
            return None                                      # «купил 500 за 120 лир»: что куплено - неизвестно
        raise ExchangeReject()                               # сумма без валюты или валюта без суммы
    give_cur = give[2][1]
    get_curs = [d[1] for _, _, d in gets]
    if give_cur in get_curs or len(set(get_curs)) != len(get_curs):
        raise ExchangeReject()                               # одна валюта с двух сторон или дважды в получении

    # способы: «с тинькофф» - отдача; прочие до первой получаемой суммы - отдача, после - ближайшей получаемой
    give_method, get_methods = None, [None] * len(gets)
    first_get = gets[0][0] if kind != "buy" else ordered[0][0]
    for s, e, t, d in items:
        if t != "M":
            continue
        code, prefix = d
        in_give = prefix in ("с", "со", "из") or (s > give[0] if kind == "buy" else s < first_get)
        if in_give:
            give_method = give_method or code
            continue
        before = [i for i, (gs, _, _) in enumerate(gets) if gs < s]
        i = before[-1] if before else 0
        get_methods[i] = get_methods[i] or code

    parts = {}
    for _, _, t, d in items:
        if t != "P":
            continue
        value, cur, target = d
        if cur not in (None, give_cur) or target not in get_curs or target in parts:
            raise ExchangeReject()
        parts[target] = _amount(value)

    fee = None
    fees = [d for _, _, t, d in items if t == "F"]
    if len(fees) > 1:
        return None
    if fees and fees[0][0] is not None and fees[0][0] > 0:
        fee = {"amount": _amount(fees[0][0]), "currency": fees[0][1] or give_cur}

    return {"kind": "exchange", "date": moneytext.date_of(low, today), "note": raw[:200],
            "give": {"amount": _amount(give[2][0]), "currency": give_cur, "method": give_method},
            "get": [{"amount": _amount(d[0]), "currency": d[1], "method": get_methods[i], "part": parts.get(d[1])}
                    for i, (_, _, d) in enumerate(gets)],
            "fee": fee}
