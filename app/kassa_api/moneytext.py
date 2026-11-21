"""Деньги в свободном тексте: разбор фразы или расшифровки голоса и детектор «похоже на деньги».

Один файл, только стандартная библиотека: бэкенд кассы использует его для разбора (parse), детектор (looks_like_money) - для
проверки «похоже на деньги». LLM нет: словари и регулярные выражения.

Правила разбора суммы (рецепт «не уверен - карточки нет»):
- кандидаты: цифры («1 500», «1,5к», «20к», «12.50»), числа прописью («пятьсот», «две тысячи пятьсот»),
  символ валюты рядом («$20»);
- время («12:00», «в 12»), даты, число перед единицей не-денег («2 часа», «3 подхода», «5 км») - не сумма;
- «по 50» (цена за штуку), «с половиной», «несколько тысяч» - не уверен: отказ;
- несколько кандидатов: берётся тот, у которого рядом названа валюта, если он один и остальные числа не похожи
  на суммы; иначе отказ;
- копейки, куруши, центы («20 лир 50 куруш», «5 рублей 50 копеек») складываются с целой частью;
- диапазоны («500-600», «от 500 до 600»), «500 или 600», «2x500», «1/2», «к вечеру» - не угадываем: отказ;
- «к» - тысяча только слитно с числом («5к»); дата вида «15.07» после предлога - не сумма;
- сотни и тысячи словами и сленгом (ревью 15.10, P1-C): «две сотни» = 200, «3 тыщи» = 3000, «полторы сотни» = 150;
  «пару сотен», «пять сотен» в родительном падеже, «сотка», «косарь», «лям» - отказ (просим цифрами), никогда 2 или 3;
- несколько сумм без валюты и без «за» («кофе 150 булка 80») - отказ с просьбой разделить сообщение.
"""
import re
from datetime import date as date_cls, timedelta
from decimal import Decimal, InvalidOperation

MAX_UNITS = 1_000_000_000


class Reject(Exception):
    """Код: no_amount (суммы нет) или unsure_amount (число есть, но однозначной суммы нет)."""

    def __init__(self, code, several=False):
        super().__init__(code)
        self.code = code
        self.several = several              # в сообщении несколько сумм: ответ просит разделить его


# --- словари ---------------------------------------------------------------------------------------------------
ONES = {"ноль": 0, "один": 1, "одна": 1, "одну": 1, "одного": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
        "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9}
TEENS = {"десять": 10, "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
         "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18, "девятнадцать": 19}
TENS = {"двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50, "шестьдесят": 60, "семьдесят": 70,
        "восемьдесят": 80, "девяносто": 90}
HUNDREDS = {"сто": 100, "двести": 200, "триста": 300, "четыреста": 400, "пятьсот": 500, "шестьсот": 600,
            "семьсот": 700, "восемьсот": 800, "девятьсот": 900}
THOUSAND_FORMS = {"тысяча", "тысячи", "тысяч", "тысячу", "тыща", "тыщи", "тыщу", "тыщ"}
SOTNYA_FORMS = {"сотня", "сотни", "сотен", "сотню"}
SLANG_BEFORE_AMOUNT = ("сотк", "косар", "лям")      # «две сотки», «косарь», «лям»: слэнг, смысл неоднозначен
MILLION_FORMS = {"миллион", "миллиона", "миллионов"}
HALF_FORMS = {"полтора", "полторы"}
NUM_WORDS = (set(ONES) | set(TEENS) | set(TENS) | set(HUNDREDS) | THOUSAND_FORMS | MILLION_FORMS | HALF_FORMS
             | SOTNYA_FORMS)
VAGUE_BEFORE = {"несколько", "пару", "пара", "много", "сколько", "столько", "немного", "мало"}

# валюта: (код, шаблон по нижнему регистру с «е» вместо «ё»); порядок важен (usdt раньше usd)
CURRENCY_RULES = [
    ("USDT", re.compile(r"usdt|юсдт|тезер")),
    ("TRY", re.compile(r"лир(?:а|ы|у|е|ой|ам|ами|ах|ов)?(?![а-я])|\btry\b|\btl\b|\bтл\b|₺")),
    ("RUB", re.compile(r"руб(?:ль|ля|лей|лю|лем|лях)?(?![а-я])|₽|\brub\b|(?<![\w])р(?![\w])")),
    ("USD", re.compile(r"бакс\w*|доллар\w*|\$|\busd\b")),
    ("EUR", re.compile(r"\bевро\b|\beur\b|€")),
    ("GBP", re.compile(r"фунт\w*|\bgbp\b|£")),
    ("KZT", re.compile(r"тенге|\bkzt\b")),
    ("AED", re.compile(r"дирхам\w*|\baed\b")),
    ("GEL", re.compile(r"\bлари\b|\bgel\b")),
    ("UAH", re.compile(r"гривн\w*|\buah\b")),
    ("CNY", re.compile(r"юан\w*|\bcny\b")),
]
_CUR_SYMBOLS = "$€£₽₺"

# мелкая денежная единица -> валюты, у которых она бывает (сотая часть целой)
MINOR_RULES = [
    (re.compile(r"куруш\w*|курус\w*|kuru[sş]\w*"), frozenset({"TRY"})),
    (re.compile(r"коп(?:ейк\w*|еек)?"), frozenset({"RUB", "UAH"})),
    (re.compile(r"(?:евро)?цент\w*"), frozenset({"USD", "EUR"})),
    (re.compile(r"пенс\w*|пенни"), frozenset({"GBP"})),
    (re.compile(r"тетри"), frozenset({"GEL"})),
    (re.compile(r"филс\w*"), frozenset({"AED"})),
    (re.compile(r"тиын\w*"), frozenset({"KZT"})),
]

# направление: первый по тексту глагол побеждает («пришло 3000 за оплату» - поступление: «оплат» после «за» не глагол)
OUT_VERBS = re.compile(r"потрат|купил|куплен|заплат|(?<!за\s)оплат|заправ|снял|отдал|потерял"
                       r"|(?<!мне\s)вернул(?!и|ся|ась|ось|\s+мне)|расход|трат|ушло|ушли|вышло|вышли")
IN_VERBS = re.compile(r"пришл|получ|перевели|вернули|вернулся|вернулась|вернулось|мне\s+вернул|вернул\s+мне|заработ"
                      r"|прислал|выплатили|начисл|поступил|зачислил|доход")
_FROM_SOMEONE = re.compile(r"(?<!\w)[Оо]т\s+[А-ЯЁA-Z]")
# денежный глагол для детектора (спецификация разд. 8, плюс однокоренные и «снял, ушло, вышло, стоил, положил»)
MONEY_VERBS = re.compile(r"потрат|купил|заплат|оплат|получил|получила|пришл|перевели|перевёл|перевел|заработ|отдал"
                         r"|заплатила|купила|потратила|оплатила|отдала|вернули|прислал|выплатили|начисл|поступил"
                         r"|зачислил|снял|ушло|ушли|вышло|вышли|стоил|стоило|положил|внес|внёс|вернул|потерял"
                         r"|стоит|стоят|обошел|обошл|вышел|вышла|отложил|занял|одолжил|накопил|скинул|израсходовал"
                         r"|потратился|задонатил|пожертвовал",
                         re.IGNORECASE)

# слово после числа - не деньги
_UNIT_STEMS = ("час", "мин", "сек", "км", "метр", "кг", "грамм", "литр", "раз", "шт", "штук", "подход",
               "повтор", "дн", "недел", "месяц", "год", "лет", "процент", "градус", "этаж", "страниц", "глав",
               "урок", "человек", "чел", "очк", "балл", "шаг", "калор", "ккал", "день", "утра", "вечера", "ночи",
               "дня", "числа", "января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
               "сентября", "октября", "ноября", "декабря", "мл", "см", "мм", "гб", "мб", "кб", "вт", "сутки",
               "суток", "суток")
_UNIT_EXACT = {"м", "г", "л", "%", "ч", "мин."}
# слова после числа, при которых «голое» число по смыслу ещё может быть суммой («20 на обед», «300 за такси»)
_ALLOWED_NEXT = {"на", "за", "в", "во", "из", "от", "до", "для", "и", "а", "но", "да", "нет", "это", "как", "чтобы",
                 "потому", "там", "тут", "сегодня", "вчера", "позавчера", "утром", "днем", "вечером", "ночью",
                 "наличными", "картой", "карте", "нал", "кэшем", "всего", "итого", "только",
                 "примерно", "около", "точно", "ну", "вот", "было", "было.", "ушло", "ушли", "вышло", "вышли"}

# Способы человека - данные базы (таблица methods, команда kassa_api.methods), в коде нет ни одного названия.
# Встроенное только слово «наличные» для способа с кодом cash.
CASH_CODE = "cash"
CASH_RULE = re.compile(r"наличн|налом|\bнал\b|\bкэш|cash")
# слова названий, по которым один способ от другого не отличить: само по себе такое слово способ не выбирает
_GENERIC_TITLE_WORDS = {"карта", "карту", "карты", "картой", "банк", "банка", "биржа", "биржи", "кошелек", "кошелька",
                        "счет", "счета", "счету", "наличные", "наличными", "основной", "основная", "другой", "другая"}
_WORD_RE = re.compile(r"[a-zа-я0-9]+")


def _title_keys(title: str):
    return [w for w in _WORD_RE.findall(norm(title)) if len(w) >= 3 and w not in _GENERIC_TITLE_WORDS]


def method_spans(low: str, methods=None):
    """Упоминания способов в тексте -> [(начало, конец, код, область)], без пересечений, длинные первыми.
    methods - список способов человека [{code, title, scope}] (None: только «наличные»).
    Правило: название целиком (без учёта регистра) или его отличительное слово («на карту Revolut» -> «Карта
    Revolut»); слово, подходящее двум способам, не выбирает ни один."""
    found = []                                              # (начало, конец, код, область)
    by_code = {m["code"]: m for m in methods or []}
    if methods is None or CASH_CODE in by_code:
        scope = by_code[CASH_CODE]["scope"] if CASH_CODE in by_code else "both"
        found += [(m.start(), m.end(), CASH_CODE, scope) for m in CASH_RULE.finditer(low)]
    keyed = {}                                              # слово -> коды способов, которым оно отличительное
    for m in methods or []:
        title = norm(m["title"])
        if not title or m["code"] == CASH_CODE:
            continue
        for hit in re.finditer(r"(?<![a-zа-я0-9])" + re.escape(title) + r"(?![a-zа-я0-9])", low):
            found.append((hit.start(), hit.end(), m["code"], m["scope"]))
        for key in _title_keys(m["title"]):
            keyed.setdefault(key, set()).add(m["code"])
    for hit in _WORD_RE.finditer(low):
        word = hit.group(0)
        owners = set()
        for key, codes in keyed.items():
            if word == key or (len(key) >= 5 and len(word) >= 5 and word.startswith(key[:-1])):
                owners |= codes
        if len(owners) == 1:
            code = next(iter(owners))
            found.append((hit.start(), hit.end(), code, by_code[code]["scope"]))
    found.sort(key=lambda f: (-(f[1] - f[0]), f[0]))
    taken, out = [], []
    for f in found:
        if all(f[1] <= t[0] or f[0] >= t[1] for t in taken):
            taken.append(f)
            out.append(f)
    return sorted(out)


_ISO_DATE = re.compile(r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)")
_DMY_DATE = re.compile(r"(?<![\d.,])\d{1,2}\.\d{1,2}\.\d{2,4}(?![\d])")
_TIME = re.compile(r"(?<![\d:])\d{1,2}:\d{2}(?::\d{2})?(?![\d:])")
_DM_DATE = re.compile(r"(\d{1,2})\.(\d{2})")                     # «15.07», но не «8.5» и не «12.50» (месяц 50)
_SP = "    "
_DIGITS = re.compile(
    r"(?<![\d.,])(?P<num>\d{1,3}(?:[" + _SP + r"]\d{3})+(?:[.,]\d{1,2})?"
    r"|\d{1,3}(?:\.\d{3})+,\d{1,2}|\d{1,3}(?:,\d{3})+\.\d{1,2}|\d+(?:[.,]\d+)?)"
    # «к» и «k» - тысяча только слитно с числом («5к»); «тыс», «млн» - и через пробел
    r"(?P<sfx>(?:[кk](?!\w)|\s?(?:тыс\w*|тыщ\w*|млн|миллион\w*|сот(?:ня|ни|ен|ню))(?!\w)))?")
_WORD = re.compile(r"[a-zа-я]+(?:-[a-zа-я]+)?")
# неоднозначные записи суммы: диапазон, «или», множитель, дробь, сумма слагаемых
_AMBIGUOUS = [
    re.compile(r"(?<![\d.,])\d+(?:[.,]\d+)?[кk]?\s*[-–—−]\s*\d+"),
    re.compile(r"\bот\s+\d[\d\s.,]*?(?:[кk]|тыс\w*)?\s*(?:[^\W\d_]{1,10}\.?\s+)?до\s+\d[\d.,]*"),
    re.compile(r"\d[\d.,]*(?:[кk]|\s?тыс\w*)?\s*(?:[^\W\d_]{1,10}\.?\s+)?или\s+\d[\d.,]*"),
    re.compile(r"\d\s*[xх×*]\s*\d"),
    re.compile(r"\d\s*/\s*\d"),
    re.compile(r"\d\s*\+\s*\d"),
    re.compile(r"\d\s*(?:р|руб\w*|₽)\.?\s+\d+\s*к(?!\w)"),      # «5р 50к»: копейки или тысячи - не угадываем
]
# предлоги, после которых «15.07» - дата
_DATE_PREPS = {"на", "до", "с", "к", "от", "числа", "после", "перед", "в", "во", "дата", "срок", "по"}
# слово перед голым числом, при котором оно скорее всего не сумма («вес 72,5», «день 3»)
_NOT_MONEY_PREV = {"в", "во", "к", "ко", "до", "после", "перед", "с", "со", "от", "около", "примерно", "ровно", "где-то",
                   "из", "у", "для", "про", "при", "над", "под", "без", "через", "по", "и", "а", "но", "мне", "ему", "ей",
                   "нам", "день", "дня", "неделя", "недели", "месяц", "глава", "урок", "часть", "раунд", "уровень",
                   "этаж", "комната", "квартира", "кв", "дом", "подъезд", "вес", "рост", "пульс", "давление",
                   "температура", "возраст", "номер", "тел", "телефон", "код", "пин", "шаги", "сон", "счет", "счёт",
                   "id", "версия", "страница", "стр", "серия", "сезон", "курс", "цена", "размер"}
_PAST_VERB = re.compile(r"(?:л|ла|ли|ло|лся|лась|лись|лось)$")
_MONEY_PREV = {"за", "на", "стоил", "стоила", "стоило", "сумму", "сумма", "всего", "итого", "по"}


def norm(text) -> str:
    return " ".join(str(text or "").replace(" ", " ").replace(" ", " ").split()).lower().replace("ё", "е")


def _next_token(low: str, pos: int) -> str:
    m = re.match(r"\s*([^\s]+)", low[pos:])
    if not m:
        return ""
    return m.group(1).strip(".,;:!?()«»\"'")


def _prev_token(low: str, pos: int) -> str:
    m = re.search(r"([^\s]+)\s*$", low[:pos])
    if not m:
        return ""
    return m.group(1).strip(".,;:!?()«»\"'")


def _is_unit(token: str) -> bool:
    if not token:
        return False
    if token in _UNIT_EXACT:
        return True
    return token.startswith(_UNIT_STEMS)


def currency_at(low: str, start: int, end: int):
    """Валюта, названная вплотную к числу: символ перед ним или слово сразу после. -> код или None."""
    before = low[max(0, start - 2):start].strip()
    if before and before[-1] in _CUR_SYMBOLS:
        for code, rx in CURRENCY_RULES:
            if rx.search(before[-1]):
                return code
    m = re.match(r"\s?([^\s\d]{1,12})", low[end:])
    if m:
        word = m.group(1).strip(".,;:!?()")
        if word:
            for code, rx in CURRENCY_RULES:
                if rx.fullmatch(word) or (len(word) > 1 and rx.match(word) and rx.match(word).end() == len(word)):
                    return code
    return None


def currencies_in(low: str):
    """Все валюты, названные где-либо в тексте (множество кодов)."""
    found = []
    for code, rx in CURRENCY_RULES:
        if rx.search(low):
            found.append(code)
    return found


# --- числа прописью --------------------------------------------------------------------------------------------
def _words_value(words):
    """Список слов -> Decimal или None (если грамматика не сошлась)."""
    if not words:
        return None
    if words[0] in HALF_FORMS:
        rest = words[1:]
        if rest and rest[0] in THOUSAND_FORMS and len(rest) == 1:
            return Decimal(1500)
        if rest and rest[0] in MILLION_FORMS and len(rest) == 1:
            return Decimal(1500000)
        if rest and rest[0] in SOTNYA_FORMS and len(rest) == 1:
            return Decimal(150)
        return Decimal("1.5") if not rest else None
    total = 0
    group = 0
    stage = 0                 # 0 - ничего; 1 - сотни; 2 - десятки/«-надцать»; 3 - единицы
    seen_thousand = False
    seen_million = False
    seen_sotnya = False
    for w in words:
        if w in HUNDREDS:
            if stage >= 1 or seen_sotnya:
                return None
            group += HUNDREDS[w]
            stage = 1
        elif w in TEENS:
            if stage >= 2:
                return None
            group += TEENS[w]
            stage = 3
        elif w in TENS:
            if stage >= 2:
                return None
            group += TENS[w]
            stage = 2
        elif w in ONES:
            if stage >= 3:
                return None
            group += ONES[w]
            stage = 3
        elif w in SOTNYA_FORMS:
            if seen_sotnya or seen_million:
                return None
            if group == 0:
                if w not in ("сотня", "сотню"):
                    return None       # «сотен» без числа перед ним («пару сотен»): не уверен
                group = 1
            elif not (stage == 3 and group <= 9):
                return None           # «двадцать сотен», «сто сотен»: не угадываем
            total += group * 100
            group = 0
            stage = 0
            seen_sotnya = True
        elif w in THOUSAND_FORMS:
            if seen_thousand or seen_million or seen_sotnya:
                return None
            if group == 0:
                if w not in ("тысяча", "тысячу", "тыща", "тыщу"):
                    return None       # «тысяч» без числа перед ним («несколько тысяч»): не уверен
                group = 1
            total += group * 1000
            group = 0
            stage = 0
            seen_thousand = True
        elif w in MILLION_FORMS:
            if seen_million or seen_thousand or seen_sotnya:
                return None
            if group == 0:
                if w != "миллион":
                    return None
                group = 1
            total += group * 1_000_000
            group = 0
            stage = 0
            seen_million = True
        else:
            return None
    return Decimal(total + group)


def _word_candidates(low: str):
    """Числа прописью: список (start, end, Decimal|None). None - слова числа есть, но однозначно не сошлись."""
    toks = [(m.group(0), m.start(), m.end()) for m in _WORD.finditer(low)]
    out = []
    i = 0
    while i < len(toks):
        w, s, e = toks[i]
        if w in NUM_WORDS:
            j = i
            while j + 1 < len(toks) and toks[j + 1][0] in NUM_WORDS and low[toks[j][2]:toks[j + 1][1]].strip() == "":
                j += 1
            words = [t[0] for t in toks[i:j + 1]]
            value = _words_value(words)
            vague = i > 0 and toks[i - 1][0] in VAGUE_BEFORE and low[toks[i - 1][2]:s].strip() == ""
            out.append((s, toks[j][2], None if vague else value))
            i = j + 1
        else:
            i += 1
    return out


# --- кандидаты суммы -------------------------------------------------------------------------------------------
def _digit_value(num: str, sfx: str):
    s = re.sub("[" + _SP + "]", "", num)
    if "," in s and "." in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thou = "." if dec == "," else ","
        s = s.replace(thou, "").replace(dec, ".")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = head + "." + tail
    if re.fullmatch(r"\d+[.]\d{3}", s) and not sfx:
        return None                          # «12,345»: тысячи или дробь - не угадываем
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    if sfx:
        sf = sfx.strip()
        value *= 1_000_000 if sf.startswith(("млн", "миллион")) else 100 if sf.startswith("сот") else 1000
    return value


def _minor_codes(token: str):
    for rx, codes in MINOR_RULES:
        if rx.fullmatch(token):
            return codes
    return None


def _mask_text(low: str) -> str:
    masked = list(low)
    for rx in (_ISO_DATE, _DMY_DATE, _TIME):
        for m in rx.finditer(low):
            for k in range(m.start(), m.end()):
                masked[k] = " "
    return "".join(masked)


def _merge_minor(text: str, out: list) -> list:
    """«20 лир 50 куруш» -> одно число 20.50 (копейки/куруши/центы складываются с целой частью). Одинокая мелкая
    единица («50 куруш») - сумма в сотых долях."""
    merged = []
    for c in out:
        codes = None if c["sfx"] else _minor_codes(_next_token(text, c["end"]))
        if codes is None:
            merged.append(c)
            continue
        mw = re.match(r"\s*([^\s]+)", text[c["end"]:])
        word_end = c["end"] + mw.end()
        minor_ok = c["value"] is not None and c["value"] == c["value"].to_integral_value() and 0 <= c["value"] < 100
        prev = merged[-1] if merged else None
        prev_cur = currency_at(text, prev["start"], prev["end"]) if prev is not None else None
        if prev is not None and prev_cur in codes and not prev.get("minor_only") \
                and re.fullmatch(r"\s*[^\W\d_]{0,12}\.?\s*(?:и\s+)?", text[prev["end"]:c["start"]]):
            if minor_ok and prev["value"] is not None:
                prev["value"] = prev["value"] + c["value"] / 100
            else:
                prev["value"], prev["bad"] = None, True
            prev["end"] = word_end
            prev["cur_override"] = prev_cur
            continue
        c["end"] = word_end
        c["minor_only"] = True
        if minor_ok and len(codes) == 1:
            c["value"] = c["value"] / 100
            c["cur_override"] = next(iter(codes))
        else:
            c["value"], c["bad"] = None, True
        merged.append(c)
    return merged


def candidates(low: str):
    """Список кандидатов суммы: dict(start, end, value|None, cur, kind, unit, per, half, dm, kword, big, sfx, ...)."""
    text = _mask_text(low)
    out = []
    for m in _DIGITS.finditer(text):
        value = _digit_value(m.group("num"), m.group("sfx"))
        out.append({"start": m.start(), "end": m.end(), "value": value, "kind": "digits", "num": m.group("num"),
                    "sfx": bool(m.group("sfx")), "bad": value is None})
    digit_spans = [(c["start"], c["end"]) for c in out]
    for s, e, value in _word_candidates(text):
        if any(s < de and e > ds for ds, de in digit_spans):
            continue                         # слово-множитель после цифры («2 тысячи») уже учтено в числе
        out.append({"start": s, "end": e, "value": value, "kind": "words", "num": "", "sfx": False,
                    "bad": value is None})
    out.sort(key=lambda c: c["start"])
    out = _merge_minor(text, out)
    for c in out:
        c["cur"] = c.get("cur_override") or currency_at(text, c["start"], c["end"])
        nxt = _next_token(text, c["end"])
        prv = _prev_token(text, c["start"])
        c["next"] = nxt
        c["prev"] = prv
        dm = _DM_DATE.fullmatch(c["num"]) if c["kind"] == "digits" and not c["sfx"] else None
        c["dm"] = bool(dm and 1 <= int(dm.group(1)) <= 31 and 1 <= int(dm.group(2)) <= 12)
        c["date_word"] = c["dm"] and c["cur"] is None and prv in _DATE_PREPS      # «на 15.07»
        c["unit"] = (_is_unit(nxt) and c["cur"] is None) or c["date_word"]
        c["per"] = prv == "по"
        c["half"] = nxt == "с" and text[c["end"]:].lstrip().startswith(("с половиной", "с полтиной"))
        c["kword"] = nxt == "к"                                                      # «10 к вечеру»
        c["big"] = c["kind"] == "digits" and not c["sfx"] and len(re.sub(r"\D", "", c["num"])) >= 8
        timeish = (prv in ("в", "к", "до", "после") and c["cur"] is None and c["kind"] == "digits"
                   and c["value"] is not None and c["value"] <= 24)
        c["unit"] = c["unit"] or timeish
        # слэнг после числа («две сотки», «косарь», «лям»): смысл неоднозначен, просим цифрами
        if nxt.startswith(SLANG_BEFORE_AMOUNT) and not c["unit"]:
            c["value"], c["bad"], c["slang"] = None, True, True
        # «150 150» склеился бы в 150150: без названной валюты две группы по три цифры - не угадываем
        if c["kind"] == "digits" and not c["sfx"] and c["cur"] is None and _spaced_ambiguous(c["num"]):
            c["value"], c["bad"] = None, True
    return out


def _spaced_ambiguous(num: str) -> bool:
    groups = re.split("[" + _SP + "]", re.split(r"[.,]", num)[0])
    return len(groups) > 1 and len(groups[0]) == 3 and any(g != "000" for g in groups[1:])


def ambiguous_spans(low: str):
    """Участки текста с диапазоном, «или», множителем, дробью, суммой слагаемых."""
    text = _mask_text(low)
    return [m.span() for rx in _AMBIGUOUS for m in rx.finditer(text)]


def _overlaps(spans, c) -> bool:
    return any(s < c["end"] and e > c["start"] for s, e in spans)


def _fmt_amount(value: Decimal) -> str:
    q = value.quantize(Decimal("0.01"))
    if q != value:
        raise Reject("unsure_amount")
    text = format(q, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _money_like(c, text: str) -> bool:
    """Похоже ли число на ещё одну сумму («3000 за еду»), а не на курс или количество («2 за 500 лир»)."""
    if c["cur"]:
        return True
    return c["next"] == "за" and not re.match(r"\s*за\s+\d", text[c["end"]:])


def pick_amount(low: str):
    """Выбор суммы. -> (Decimal value, currency|None, span) или Reject."""
    cands = candidates(low)
    if not cands:
        raise Reject("no_amount")
    live = [c for c in cands if not c["unit"]]
    if not live:
        raise Reject("no_amount")
    # числа слов без однозначного значения («несколько тысяч», «три с половиной») - не уверен
    if any(c["bad"] or c["half"] or c["kword"] for c in live):
        raise Reject("unsure_amount")
    spans = ambiguous_spans(low)
    if any(_overlaps(spans, c) for c in live):
        raise Reject("unsure_amount")
    if len(live) > 1:
        live = [c for c in live if not c["dm"]] or live
    if len(live) > 1:
        with_cur = [c for c in live if c["cur"]]
        after_za = [c for c in live if c["prev"] == "за"]
        if len(with_cur) == 1:
            if any(_money_like(c, low) for c in live if c is not with_cur[0]):
                raise Reject("unsure_amount", several=True)
            # «кофе 150 булка 80 тл»: валюта у одной суммы, а вторая тоже «название число» - не угадываем
            if _pair_money(low, with_cur[0]) and any(_pair_money(low, c) for c in live if c is not with_cur[0]):
                raise Reject("unsure_amount", several=True)
            live = with_cur
        elif not with_cur and len(after_za) == 1:
            live = after_za
        else:
            raise Reject("unsure_amount", several=True)
    pick = live[0]
    if pick["per"]:
        raise Reject("unsure_amount")
    if pick["dm"] and pick["cur"] is None and not MONEY_VERBS.match(pick["prev"]):
        raise Reject("unsure_amount")                   # «кофе 15.07»: дата или сумма - не угадываем
    value = pick["value"]
    if value is None or value <= 0 or value > MAX_UNITS:
        raise Reject("unsure_amount")
    return value, pick["cur"], (pick["start"], pick["end"])


def direction_of(text: str, default: str = "out") -> str:
    """Направление: ведущий знак (+/-/плюс/минус) > первый по тексту глагол прихода/расхода > «от <Имя>» > default."""
    s = str(text or "")
    m = re.match(r"^\s*([+\-−–—])\s*(?=\d)", s)
    if m:
        return "in" if m.group(1) == "+" else "out"
    low = norm(s)
    if re.match(r"^плюс\s+(?=\d)", low):
        return "in"
    if re.match(r"^минус\s+(?=\d)", low):
        return "out"
    out_m, in_m = OUT_VERBS.search(low), IN_VERBS.search(low)
    if out_m and in_m:
        return "out" if out_m.start() < in_m.start() else "in"
    if out_m:
        return "out"
    if in_m:
        return "in"
    if _FROM_SOMEONE.search(s):
        return "in"
    return default


def method_of(low: str, direction: str, methods=None):
    """Способ из списка человека по тексту; не нашли - None («не выбран»)."""
    for _, _, code, scope in sorted(method_spans(low, methods), key=lambda f: (-(f[1] - f[0]), f[0])):
        if scope in (direction, "both"):
            return code
    return None


def date_of(low: str, today: str) -> str:
    d = date_cls.fromisoformat(today)
    if re.search(r"\bпозавчера\b", low):
        return (d - timedelta(days=2)).isoformat()
    if re.search(r"\bвчера\b", low):
        return (d - timedelta(days=1)).isoformat()
    return today


def parse(text, today: str, default_direction: str = "out", default_currency: str | None = None,
          methods=None) -> dict:
    """Разбор фразы -> dict(direction, amount (строка), currency, currency_guess, note, date, method).
    Бросает Reject('no_amount'|'unsure_amount')."""
    raw = " ".join(str(text or "").split())
    low = norm(raw)
    if not low:
        raise Reject("no_amount")
    value, cur, _ = pick_amount(low)
    direction = direction_of(raw, default_direction)
    guess = False
    if cur is None:
        named = currencies_in(low)
        if len(named) == 1:
            cur = named[0]
        elif len(named) > 1:
            raise Reject("unsure_amount")
    if cur is None:
        cur = default_currency
        guess = True
    return {"direction": direction, "amount": _fmt_amount(value), "currency": cur, "currency_guess": guess,
            "note": raw[:200], "date": date_of(low, today), "method": method_of(low, direction, methods)}


# --- детектор ---------------------------------------------------------------------------------------
MAX_WORDS = 12
DIARY_PREFIX = re.compile(r"^\s*дневник\s*[:\-—–]\s*", re.IGNORECASE)
_SENT_SPLIT = re.compile(r"(?<=[.!?;])\s+")
_CLAUSE_SPLIT = re.compile(r",\s+|\s+(?:и|а|но|потом|затем|ещё|еще)\s+|\s[-—–]\s")
_LEAD_SIGN = {"+", "-", "−", "–", "—"}


def strip_diary_prefix(text: str):
    """«Дневник: ...» -> текст без префикса, иначе None."""
    m = DIARY_PREFIX.match(str(text or ""))
    if not m:
        return None
    return str(text)[m.end():].lstrip()


def _chunks(text: str):
    """Короткое сообщение целиком; длинное - по предложениям и по частям предложения: денежный фрагмент ищем в
    каждой части отдельно (в длинном дневнике сумма не должна пройти мимо). -> [(часть, строгий режим)]."""
    flat = str(text).replace("\r", "\n").replace("\n", ". ")
    flat = " ".join(flat.split())
    if len(flat.split()) <= MAX_WORDS:
        return [(flat, False)]
    pieces = []
    for sent in _SENT_SPLIT.split(flat):
        if len(sent.split()) <= MAX_WORDS:
            pieces.append((sent, False))
        else:
            for part in _CLAUSE_SPLIT.split(sent):
                if part and part.strip():
                    pieces.append((part.strip(), len(part.split()) > MAX_WORDS))
    return pieces


def _verb_near(low: str, c, window: int = 40) -> bool:
    return any(0 <= c["start"] - m.end() <= window for m in MONEY_VERBS.finditer(low))


# слово из «безобидного хвоста» после суммы: способ оплаты, день, «с карты», «на двоих»
_METHOD_ANY = CASH_RULE
_TAIL_OK = {"сегодня", "вчера", "позавчера", "утром", "днем", "вечером", "ночью", "наличными", "наличкой", "картой",
            "карте", "карты", "карту", "нал", "налом", "кэшем", "с", "со", "на", "за", "итого", "всего"}
_PAIR_PREV_NOT = {"на", "за", "по"}
_SLANG_WORDS = re.compile(r"(?<![а-я])(?:косар\w*|лям\w*|полтин\w*|полтос\w*|сотк\w*|штуку)(?![а-я])")
SHORT_WORDS = 8                 # короткая строка без глагола: до стольких слов


def _prev_word(low: str, start: int) -> str:
    """Слово перед числом, пропуская тире, двоеточие и «=»: «кофе - 150» -> «кофе»."""
    head = re.sub(r"[\s\-–—−:=]+$", "", low[:start])
    m = re.search(r"([^\s]+)$", head)
    return m.group(1).strip(".,;:!?()«»\"'") if m else ""


def _prev_is_name(prv: str) -> bool:
    """Перед числом стоит слово-название («кофе 150»), а не предлог, мера или причастие («вес 72», «прошел 5»)."""
    return bool(prv) and bool(re.search(r"[a-zа-я]", prv)) and prv not in _NOT_MONEY_PREV         and not (_PAST_VERB.search(prv) and len(prv) > 3 and not MONEY_VERBS.search(prv))


def _bare_money(low: str, c) -> bool:
    """Одно живое число в короткой строке без глагола: деньги ли это (число может стоять где угодно)."""
    if c["value"] is None or c["big"] or c["dm"]:
        return False
    if low[:c["start"]].strip() in _LEAD_SIGN:
        return True                                     # «-500 такси», «+3000 зарплата»
    tail = low[c["end"]:].strip(" .,!?;:)")
    tail_words = tail.split()
    has_name = bool(re.search(r"[a-zа-я]", low[:c["start"]]))
    prv = _prev_word(low, c["start"])
    if not tail_words:                                  # число последнее: «кофе 150», «кофе - 150»
        return _prev_is_name(prv)
    nxt = tail_words[0].strip(".,;:!?()")
    tail_ok = nxt in _TAIL_OK or bool(_METHOD_ANY.search(tail))
    if has_name:                                        # «кофе 150 наличными», «такси 300 с карты»
        return _prev_is_name(prv) and tail_ok
    return tail_ok or (c["value"] >= 10 and len(tail_words) <= 2)       # «150 на кофе», «150 нал», «150 кофе»


def _pair_money(low: str, c) -> bool:
    """«название число» в строке с несколькими числами («кофе 150 булка 80»): число, похожее на цену."""
    if c["value"] is None or c["big"] or c["dm"] or c["value"] < 10:
        return False
    prv = _prev_word(low, c["start"])
    return _prev_is_name(prv) and prv not in _PAIR_PREV_NOT


def _chunk_is_money(raw_chunk: str, strict: bool) -> bool:
    low = norm(raw_chunk)
    if not low:
        return False
    if len(low.split()) <= SHORT_WORDS and _SLANG_WORDS.search(low):
        return True                                     # «потратил косарь», «две сотки»: сленг, разберём как отказ
    cands = candidates(low)
    live = [c for c in cands if not c["unit"]]
    if not live:
        return False
    if any(c["cur"] for c in live):                     # число с валютой: «500 лир», «$20», «500 тл»
        return True
    if any(c["sfx"] for c in live):                     # «1k», «5к», «2 тыс»
        return True
    verb = MONEY_VERBS.search(low)
    if verb and any(_overlaps(ambiguous_spans(low), c) for c in live):
        return True                                     # «заплатил 2x500»: не угадаем, но спросим
    if verb:
        for c in live:
            if c["value"] is None and not c["bad"]:
                continue
            if strict and not _verb_near(low, c):
                continue
            nxt = c["next"]
            if nxt == "" or nxt in _ALLOWED_NEXT or MONEY_VERBS.match(nxt) or c["kword"]:
                return True
            if c["kind"] == "digits" and c["prev"] in _MONEY_PREV:
                return True
    if strict:
        return False
    # без глагола: короткая строка - «кофе 150», «150 на кофе», «кофе 150 наличными», «кофе - 150», «-500 такси»,
    # «кофе 150 булка 80» (несколько сумм: карточки нет, просим разделить)
    if len(low.split()) <= SHORT_WORDS:
        if len(live) == 1:
            return _bare_money(low, live[0])
        return sum(1 for c in live if _pair_money(low, c)) >= 2
    return False


def looks_like_money(text) -> bool:
    """Похоже ли сообщение на денежную фразу (спец. разд. 8: порог настроен на перехват, ложное срабатывание - один тап).

    Ловим: число с валютой рядом («500 лир», «$20», «500 тл», «1k»); денежный глагол + число, которое не количество
    и не время; короткую строку с одним числом без глагола («кофе 150», «итого 500», «-500 такси»). Длинное
    сообщение режется на части, сумму ищем в каждой. Не ловим: «потратил три часа», «встреча в 12:00», «3 подхода»,
    даты «15.07». Исключение внутри - наружу: вызывающий код решает, что делать (ДО распознавания - пропуск)."""
    raw = " ".join(str(text or "").split())
    if not raw or raw.startswith("/") or DIARY_PREFIX.match(raw):
        return False
    return any(_chunk_is_money(piece, strict) for piece, strict in _chunks(str(text)))
