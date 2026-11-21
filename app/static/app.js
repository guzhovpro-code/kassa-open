(function () {
  "use strict";
  var tg = window.Telegram && window.Telegram.WebApp;
  var $ = function (id) { return document.getElementById(id); };

  var ERRORS = {
    bad_amount: "Сумма: только цифры и не больше двух знаков после запятой.",
    too_many_decimals: "Сумма: не больше двух знаков после запятой.",
    amount_not_positive: "Сумма должна быть больше нуля.",
    amount_too_large: "Сумма слишком большая. Проверьте нули.",
    bad_currency: "Выберите валюту.",
    missing_category: "Напишите название категории или выберите из списка.",
    bad_category: "Категория: до 40 символов, без служебных знаков.",
    bad_note: "Заметка: до 200 символов.",
    bad_date: "Выберите дату.",
    date_in_future: "Дата не может быть в будущем.",
    date_too_old: "Дата старше года назад. Внесите её другим способом.",
    method_required: "Выберите способ.",
    month_closed: "Этот месяц закрыт: записи за него не вносятся. Итог месяца задаёт закрытие.",
    record_closed: "Запись относится к закрытому месяцу: изменить её нельзя.",
    unknown_method: "Такого способа нет. Выберите из списка.",
    bad_new_method: "Назовите способ по-своему, слово «другой» не годится.",
    bad_new_method_len: "Название способа: до 30 символов.",
    idem_conflict: "Эта форма уже отправлялась с другими данными. Проверьте записи и внесите заново.",
    not_found: "Такой записи нет.",
    record_cancelled: "Запись удалена. Сначала верните её.",
    field_not_editable: "Это поле менять нельзя.",
    confirm_required: "Проверьте сумму ещё раз.",
    draft_closed: "Этот черновик уже закрыт (записан, отменён или устарел). Дубля нет.",
    refused: "Не получилось записать. Внесите вручную.",
    bad_request: "Не получилось. Проверьте поля.",
    bad_period: "Такого периода нет. Проверьте даты: «с» не позже «по», будущее выбрать нельзя.",
    period_too_long: "Период не длиннее 400 дней.",
    bad_segment: "Сегмент: B2B или B2C, и только у поступления.",
    bad_payer: "Плательщик: до 30 символов, без служебных знаков.",
    server_error: "Касса не смогла записать. Ничего не записано, попробуйте позже."
  };

  var MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь",
                "ноябрь", "декабрь"];
  var MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
                    "ноября", "декабря"];

  var state = {
    tab: "record",
    dir: "out", cur: { out: "TRY", in: "TRY" },
    catManual: null, catOther: false, catAuto: null,     // категория: выбрана рукой / «Другая» / определена по тексту
    method: "", methodOther: false, methodBy: { out: "", in: "" }, showMoreMethod: false, syncMethod: false,
    dateKey: "today", datePick: "", showMoreCur: false, pending: [], clientId: null, busy: false, draftId: null,
    meta: null, catalog: [], lastId: null, month: null, recentFilter: "", lockUntil: 0,
    segment: "",                                    // сегмент поступления в форме: "b2b" | "b2c" | "" (без пометки)
    x: null, exMonth: null, lastKind: ""            // обмен: поля формы, месяц списка «Обмены», что отменяет «↩ Отменить»
  };
  var initData = tg ? (tg.initData || "") : "";

  function show(el, on) { el.hidden = !on; }
  function setText(el, text) {
    el.textContent = text; show(el, !!text);
    if (text) { window.scrollTo(0, 0); }      // сообщения сверху формы: видны без прокрутки
  }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) { e.className = cls; }
    if (text !== undefined) { e.textContent = text; }
    return e;
  }
  function clear(node) { node.textContent = ""; }

  function uuid() {
    if (window.crypto && window.crypto.randomUUID) { return window.crypto.randomUUID(); }
    var b = new Uint8Array(16);
    window.crypto.getRandomValues(b);
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    var h = Array.prototype.map.call(b, function (x) { return ("0" + x.toString(16)).slice(-2); }).join("");
    return h.slice(0, 8) + "-" + h.slice(8, 12) + "-" + h.slice(12, 16) + "-" + h.slice(16, 20) + "-" + h.slice(20);
  }

  function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign({ "Authorization": "tma " + initData }, opts.headers || {});
    opts.cache = "no-store";
    return fetch(path, opts).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) { return { status: r.status, body: j }; });
    });
  }
  function postJson(path, data) {
    return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data) });
  }

  // --- даты (строки YYYY-MM-DD, часовой пояс сервера) ---
  function shiftDate(iso, days) {
    var p = iso.split("-");
    var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2]));
    d.setUTCDate(d.getUTCDate() + days);
    return d.toISOString().slice(0, 10);
  }
  function chosenDate() {
    var today = state.meta.today;
    if (state.dateKey === "today") { return today; }
    if (state.dateKey === "yesterday") { return shiftDate(today, -1); }
    return state.datePick;
  }
  function shiftMonth(key, delta) {
    var y = +key.slice(0, 4), m = +key.slice(5, 7) - 1 + delta;
    y += Math.floor(m / 12); m = ((m % 12) + 12) % 12;
    return y + "-" + ("0" + (m + 1)).slice(-2);
  }
  function monthTitle(key) {
    var t = MONTHS[+key.slice(5, 7) - 1];
    return t.charAt(0).toUpperCase() + t.slice(1) + " " + key.slice(0, 4);
  }
  function dayTitle(iso) { return +iso.slice(8, 10) + " " + MONTHS_GEN[+iso.slice(5, 7) - 1]; }
  function shortDate(iso) { return iso.slice(8, 10) + "." + iso.slice(5, 7); }

  // --- деньги: на сервере целые центы ---
  function fmtCents(c, signed) {
    var neg = c < 0, a = Math.abs(c);
    var whole = Math.floor(a / 100), frac = a % 100;
    var s = String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
    if (frac) { s += "," + ("0" + frac).slice(-2); }
    return (neg ? "−" : (signed ? "+" : "")) + s;
  }

  // --- чипсы ---
  function chips(container, items, selected, onPick) {
    clear(container);
    items.forEach(function (it) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "chip" + (it.value === selected ? " on" : "");
      b.textContent = it.label;
      b.addEventListener("click", function () { onPick(it.value); });
      container.appendChild(b);
    });
  }

  function touched() {
    // любая правка поля сбрасывает подтверждения и предупреждение
    state.pending = [];
    setText($("warn"), ""); setText($("err"), ""); show($("ok"), false);
    setButton("Записать");
  }

  function methodChosen() {
    return state.methodOther ? !!$("method-other").value.trim() : !!state.method;
  }

  function dateClosed() {
    if (!state.meta) { return false; }
    var d = chosenDate();
    return !!d && (state.meta.closed_months || []).indexOf(d.slice(0, 7)) >= 0;
  }

  // «Записать» (нативная кнопка Telegram) нажимается, только когда выбран способ и месяц даты не закрыт
  function refreshButton() {
    show($("method-hint"), !methodChosen());
    if (!tg || !tg.MainButton) { return; }
    if (state.tab === "record" && state.dir === "ex") {
      if (exDateClosed()) { tg.MainButton.disable(); } else { tg.MainButton.enable(); }
      return;
    }
    if (state.tab === "record" && (!methodChosen() || dateClosed())) { tg.MainButton.disable(); }
    else { tg.MainButton.enable(); }
  }

  function setButton(text) {
    if (text === "Записать" && state.dir === "ex" && state.x && state.x.editId) { text = "Сохранить"; }
    if (tg && tg.MainButton) { tg.MainButton.setText(text); }
    refreshButton();
  }

  // --- поиск валюты: по коду и по русскому названию ---------------------------------------------------------
  function norm(s) { return String(s).toLowerCase().replace(/ё/g, "е").replace(/\s+/g, " ").trim(); }
  function stemOf(word) { return word.length >= 4 ? word.replace(/[аяыиеоуьй]$/, "") : word; }

  // меньше - лучше; null - не подходит. Токены запроса должны найтись все.
  function scoreCurrency(c, tokens, mainIdx) {
    var code = c.code.toLowerCase();
    var nameWords = norm(c.name).split(/[\s()\-]+/).filter(Boolean);
    var otherWords = c.k.split(/[\s()\-]+/).filter(Boolean);
    var total = 0;
    for (var i = 0; i < tokens.length; i++) {
      var t = tokens[i], best = null;
      var take = function (v) { if (best === null || v < best) { best = v; } };
      if (code === t) { take(0); } else if (code.indexOf(t) === 0) { take(1); }
      nameWords.forEach(function (w) {
        if (w.indexOf(t) === 0) { take(2); }
        else if (t.length >= 3 && w.length >= 4 && t.indexOf(stemOf(w)) === 0 && stemOf(w).length >= 3) { take(3); }
      });
      otherWords.forEach(function (w) {
        if (w.indexOf(t) === 0) { take(4); }
        else if (t.length >= 3 && w.length >= 4 && t.indexOf(stemOf(w)) === 0 && stemOf(w).length >= 3) { take(5); }
      });
      if (best === null) { return null; }
      total += best;
    }
    return total * 10 + (mainIdx < 0 ? 9 : mainIdx);          // при равенстве основные валюты выше
  }

  function searchCurrencies(query) {
    var q = norm(query);
    var main = state.meta ? state.meta.currencies.main.concat(state.meta.currencies.second || []) : [];
    if (!q) {
      return state.catalog.slice().sort(function (a, b) {
        var ma = main.indexOf(a.code), mb = main.indexOf(b.code);
        if ((ma < 0) !== (mb < 0)) { return ma < 0 ? 1 : -1; }
        return ma !== mb ? ma - mb : (a.code < b.code ? -1 : 1);
      });
    }
    var tokens = q.split(" ");
    var scored = [];
    state.catalog.forEach(function (c) {
      var s = scoreCurrency(c, tokens, main.indexOf(c.code));
      if (s !== null) { scored.push({ c: c, s: s }); }
    });
    scored.sort(function (a, b) { return a.s - b.s || (a.c.code < b.c.code ? -1 : 1); });
    return scored.map(function (x) { return x.c; });
  }

  function pickCurrency(code) {
    state.cur[state.dir] = code;          // запоминается как валюта направления; после записи её отдаёт last_currency
    state.showMoreCur = false;
    $("cur-search").value = "";
    touched(); render();
  }

  function renderResults() {
    var box = $("cur-results");
    clear(box);
    var list = searchCurrencies($("cur-search").value);
    if (!list.length) { box.appendChild(el("div", "empty", "Ничего не найдено")); return; }
    list.forEach(function (c) {
      var b = el("button", "row" + (c.code === state.cur[state.dir] ? " on" : ""));
      b.type = "button";
      b.setAttribute("role", "option");
      b.setAttribute("data-code", c.code);
      b.appendChild(el("span", "code", c.code));
      b.appendChild(el("span", "nm", c.name));
      b.addEventListener("click", function () { pickCurrency(c.code); });
      box.appendChild(b);
    });
  }

  function renderCurrency() {
    var meta = state.meta;
    var main = meta.currencies.main;
    var cur = state.cur[state.dir];
    var items = main.map(function (c) { return { value: c, label: c }; });
    if (main.indexOf(cur) < 0) { items.push({ value: cur, label: cur }); }     // выбранная не из основных
    items.push({ value: "__more", label: state.showMoreCur ? "Ещё ▴" : "Ещё ▾" });
    chips($("cur"), items, cur, function (v) {
      if (v === "__more") {
        state.showMoreCur = !state.showMoreCur;
        render();
        if (state.showMoreCur) { $("cur-search").focus(); }
        return;
      }
      state.cur[state.dir] = v; touched(); render();
    });
    show($("cur-panel"), state.showMoreCur);
    if (!state.showMoreCur) { return; }
    var recent = meta.currencies.recent || [];
    show($("cur-recent-block"), recent.length > 0);
    chips($("cur-recent"), recent.map(function (c) { return { value: c, label: c }; }), cur, pickCurrency);
    renderResults();
  }

  // --- категория: определяется по тексту «на что» (сервер, словарь), рукой меняется выпадающим списком --------------
  var catTimer = null, catSeq = 0;

  function defaultCategory() { return state.meta.defaults[state.dir]; }

  function renderCategory() {
    var meta = state.meta, isOut = state.dir === "out";
    var sel = $("cat-select");
    var shown = state.catOther ? "__other" : (state.catManual !== null ? state.catManual : (state.catAuto || defaultCategory()));
    var names = meta.categories[state.dir].slice();
    if (shown !== "__other" && names.indexOf(shown) < 0) { names.push(shown); }
    clear(sel);
    names.forEach(function (n) {
      var o = el("option", "", n); o.value = n; sel.appendChild(o);
    });
    if (isOut) { var other = el("option", "", "Другая…"); other.value = "__other"; sel.appendChild(other); }
    sel.value = shown;
    show($("cat-other"), state.catOther);
  }

  // подсказка категории: через 250 мс после последней буквы; ответ сервера устаревает, если текст уже другой
  function scheduleCategory(immediate) {
    clearTimeout(catTimer);
    if (state.catManual !== null || state.catOther || !state.meta) { return; }
    var text = $("note").value.trim();
    if (!text) { catSeq++; state.catAuto = defaultCategory(); renderCategory(); return; }
    var run = function () {
      var seq = ++catSeq, dir = state.dir;
      postJson("api/categorize", { text: text, direction: dir }).then(function (res) {
        if (seq !== catSeq || dir !== state.dir || state.catManual !== null || state.catOther) { return; }
        if (res.status === 200 && res.body && res.body.ok) { state.catAuto = res.body.category; renderCategory(); }
      }).catch(function () { /* не критично: сервер определит категорию сам при записи */ });
    };
    if (immediate) { run(); } else { catTimer = setTimeout(run, 250); }
  }

  function methodTitle(code) {
    var m = state.meta.methods.filter(function (x) { return x.code === code; })[0];
    return m ? m.title : code;
  }

  function pickMethod(code) {
    state.method = code; state.methodOther = false; $("method-other").value = "";
    state.methodBy[state.dir] = code;
    state.showMoreMethod = false;
    touched(); render();
  }

  // способ: чипсы последних/частых (порядок и «последний выбранный» отдаёт сервер) + «Ещё» со всем списком и «Другой»
  function renderMethod() {
    var meta = state.meta;
    var order = (meta.method_order && meta.method_order[state.dir]) || [];
    var top = meta.method_top || 4;
    var shown = order.slice(0, top);
    if (state.method && !state.methodOther && shown.indexOf(state.method) < 0 && order.indexOf(state.method) >= 0) {
      shown.push(state.method);                              // выбранный из «Ещё» остаётся виден чипсом
    }
    var open = state.showMoreMethod || state.methodOther;
    var items = shown.map(function (c) { return { value: c, label: methodTitle(c) }; });
    items.push({ value: "__more", label: open ? "Ещё ▴" : "Ещё ▾" });
    chips($("method"), items, state.methodOther ? "" : state.method, function (v) {
      if (v === "__more") {
        state.showMoreMethod = !open;
        if (!state.showMoreMethod && state.methodOther && !$("method-other").value.trim()) { state.methodOther = false; }
        render();
        return;
      }
      pickMethod(v);
    });
    show($("method-panel"), open);
    var msel = $("method-select");
    clear(msel);
    var ph = el("option", "", "Выберите способ"); ph.value = ""; msel.appendChild(ph);
    order.forEach(function (c) { var o = el("option", "", methodTitle(c)); o.value = c; msel.appendChild(o); });
    var oo = el("option", "", "Другой…"); oo.value = "__other"; msel.appendChild(oo);
    msel.value = state.methodOther ? "__other" : state.method;
    show($("method-other"), state.methodOther);
    show($("method-hint"), !methodChosen());
  }

  function openDatePicker() {
    var inp = $("date-pick");
    try { if (inp.showPicker) { inp.showPicker(); } else { inp.focus(); } } catch (e) { /* поле и так видно: нажатие откроет выбор */ }
  }

  function renderDate() {
    var meta = state.meta;
    var dItems = [{ value: "today", label: "Сегодня" }, { value: "yesterday", label: "Вчера" },
                  { value: "pick", label: "Другая дата" }];
    chips($("date"), dItems, state.dateKey, function (v) {
      state.dateKey = v; touched(); render();
      if (v === "pick") { openDatePicker(); }       // «Другая дата» сразу открывает выбор даты
    });
    show($("date-pick"), state.dateKey === "pick");
    $("date-pick").max = meta.today;
    $("date-pick").min = shiftDate(meta.today, -366);
    var d = state.dateKey === "pick" && !state.datePick ? "" : chosenDate();
    $("date-label").textContent = d ? "Дата: " + dayTitle(d) : "Дата";
    var closed = dateClosed();
    $("date-hint").textContent = closed ? ERRORS.month_closed : "";
    show($("date-hint"), closed);
  }

  var SEGMENT_ITEMS = [{ value: "b2b", label: "B2B" }, { value: "b2c", label: "B2C" }];

  // сегмент: только у поступления (на экране расхода его нет); повторное нажатие на выбранный снимает пометку
  function renderSegment() {
    var on = state.dir === "in";
    show($("segment-block"), on);
    if (!on) { return; }
    chips($("segment"), SEGMENT_ITEMS, state.segment, function (v) {
      state.segment = state.segment === v ? "" : v;
      touched(); render();
    });
  }

  function render() {
    var meta = state.meta;
    Array.prototype.forEach.call($("direction").children, function (b) {
      b.className = b.getAttribute("data-dir") === state.dir ? "on" : "";
    });
    var ex = state.dir === "ex";
    show($("rec-fields"), !ex);
    show($("xform"), ex);
    if (ex) { renderExchangeForm(); setButton("Записать"); return; }

    renderCurrency();
    renderCategory();
    renderDate();
    renderMethod();
    renderSegment();
    refreshButton();
  }

  function resetAfterSave() {
    $("amount").value = ""; $("note").value = "";
    state.catManual = null; state.catOther = false; $("cat-other").value = ""; state.catAuto = null;
    clearTimeout(catTimer); catSeq++;
    // способ остаётся тем, что выбрали (запоминается); «Другой» после записи сервер отдаёт как последний (loadMeta)
    if (state.methodOther) { state.method = ""; state.methodOther = false; state.syncMethod = true; }
    $("method-other").value = "";
    state.showMoreMethod = false;
    state.dateKey = "today"; state.datePick = ""; $("date-pick").value = "";
    state.segment = "";
    state.clientId = uuid();
    state.pending = [];
    state.draftId = null;
    show($("draft-banner"), false);
    render();                                       // форма перерисована сразу, не после ответа /api/meta
    setButton("Записать");
  }

  var AMOUNT_RE = /^[0-9]{1,12}([.,][0-9]{1,2})?$/;
  function amountError(text) {
    var raw = String(text).replace(/[\s  ]/g, "");
    if (!AMOUNT_RE.test(raw)) {
      return /^[0-9]{1,12}[.,][0-9]{3,}$/.test(raw) ? "too_many_decimals" : "bad_amount";
    }
    if (/^0+([.,]0+)?$/.test(raw)) { return "amount_not_positive"; }
    return null;
  }

  function validate() {
    var bad = amountError($("amount").value);
    if (bad) { return bad; }
    if (!methodChosen()) { return "method_required"; }
    if (state.dateKey === "pick" && !state.datePick) { return "bad_date"; }
    if (dateClosed()) { return "month_closed"; }
    return null;
  }

  function payload() {
    var p = {
      client_id: state.clientId, direction: state.dir, amount: $("amount").value.trim(),
      currency: state.cur[state.dir],
      note: $("note").value.trim(), date: chosenDate(), confirm: state.pending.slice()
    };
    // категорию отправляем, только если её выбрали рукой; иначе её определит сервер по тексту «на что»
    var manual = state.catOther ? $("cat-other").value.trim() : state.catManual;
    if (manual) { p.category = manual; }
    if (state.methodOther) { p.new_method = $("method-other").value.trim(); }
    else if (state.method) { p.method = state.method; }
    if (state.dir === "in" && state.segment) { p.segment = state.segment; }
    if (state.draftId) { p.draft_id = state.draftId; }
    return p;
  }

  function busy(on) {
    state.busy = on;
    if (tg && tg.MainButton) { if (on) { tg.MainButton.showProgress(true); } else { tg.MainButton.hideProgress(); } }
  }

  function haptic(kind) {
    try { if (tg && tg.HapticFeedback) { tg.HapticFeedback.notificationOccurred(kind); } } catch (e) { /* не критично */ }
  }

  var WARN_TEXT = {
    outlier: "Сумма больше чем в 10 раз выше обычного расхода в этой валюте за 60 дней. Проверьте нули.",
    possible_duplicate: "Такая же запись (направление, сумма, валюта) уже была за последние 10 минут."
  };

  function showOk(text, undoId) {
    $("ok-text").textContent = text;
    state.lastId = undoId || null;
    state.lastKind = "";
    show($("undo"), !!undoId);
    show($("ok"), true);
    window.scrollTo(0, 0);
  }

  function submit() {
    if (state.busy || state.tab !== "record") { return; }
    if (state.dir === "ex") { return submitExchange(); }
    // предупреждение о сумме надо успеть прочитать: «ещё раз» сразу после него (двойной тап) не считается
    if (state.pending.length && Date.now() < state.lockUntil) { return; }
    setText($("err"), ""); show($("ok"), false);
    var bad = validate();
    if (bad) { setText($("err"), ERRORS[bad] || "Проверьте поля."); haptic("error"); return; }
    busy(true);
    postJson("api/records", payload()).then(function (res) {
      busy(false);
      var b = res.body || {};
      if (res.status === 200 && b.ok) {
        showOk((b.duplicate_request ? "Уже записано №" : "Записано №") + b.id + (b.category ? " · " + b.category : ""),
               b.duplicate_request ? null : b.id);
        haptic("success");
        resetAfterSave();
        loadMeta(true);
        loadInbox(true);
        return;
      }
      if (res.status === 409 && b.error === "confirm_required") {
        state.pending = (b.warnings || []).slice();
        state.lockUntil = Date.now() + 2000;
        setText($("warn"), state.pending.map(function (w) { return WARN_TEXT[w] || w; }).join(" "));
        setButton(state.pending.indexOf("outlier") >= 0 ? "Да, столько" : "Записать всё равно");
        haptic("warning");
        return;
      }
      if (res.status === 401) { setText($("err"), "Откройте Кассу заново."); return; }
      if (res.status === 403) { setText($("err"), "Нет доступа."); return; }
      if (b.error === "idem_conflict") { state.clientId = uuid(); }
      setText($("err"), ERRORS[b.error] || "Не записано. Проверьте поля и попробуйте ещё раз.");
      haptic("error");
    }).catch(function () {
      busy(false);
      setText($("err"), "Нет связи. Ничего не потеряно: нажмите «Записать» ещё раз, дубля не будет.");
      haptic("error");
    });
  }

  function undoLast() {
    var id = state.lastId;
    if (!id || state.busy) { return; }
    busy(true);
    var isEx = state.lastKind === "ex";
    postJson((isEx ? "api/exchanges/" : "api/records/") + id + "/cancel", {}).then(function (res) {
      busy(false);
      if (res.status === 200 && res.body.ok) {
        showOk(isEx ? "Обмен №" + id + " отменён. Вернуть его можно во вкладке «Обмены»."
                    : "Запись №" + id + " отменена. Вернуть её можно во вкладке «Последние».", null);
        haptic("success");
        loadMeta(true);
        return;
      }
      setText($("err"), ERRORS[(res.body || {}).error] || "Не получилось отменить. Попробуйте во вкладке «Последние».");
    }).catch(function () {
      busy(false);
      setText($("err"), "Нет связи. Отмена не выполнена.");
    });
  }

  // --- отчёты и список ---------------------------------------------------------------------------------------
  function authFail(status, box) {
    if (status === 401) { box.textContent = "Откройте Кассу заново."; return true; }
    if (status === 403) { box.textContent = "Нет доступа."; return true; }
    if (status !== 200) { box.textContent = "Не удалось загрузить. Попробуйте ещё раз."; return true; }
    return false;
  }

  function renderTotals(box, rep) {
    clear(box);
    if (!rep.currencies.length) { box.appendChild(el("p", "note", "Пока пусто")); return; }
    rep.currencies.forEach(function (c) {
      var card = el("div", "card");
      card.appendChild(el("div", "card-title", c.currency));
      [["Расходы", c.out_cents, "out"], ["Поступления", c.in_cents, "in"], ["Итог", c.net_cents, "net"]]
        .forEach(function (row) {
          var line = el("div", "line" + (row[2] === "net" ? " net" : ""));
          line.appendChild(el("span", "", row[0]));
          line.appendChild(el("span", "amt", fmtCents(row[1], row[2] === "net")));
          card.appendChild(line);
        });
      box.appendChild(card);
    });
  }

  // «записи» после «для»: 1 записи, 2-4 записей, 5+ записей (родительный падеж)
  function recordsWord(n) { return (n % 10 === 1 && n % 100 !== 11) ? "записи" : "записей"; }

  // «≈ USD» не врёт нулями: у записей без курса суммы в долларах нет. Все записи периода без курса - только
  // «нет курса для N записей»; часть - суммы по остальным и отдельная пометка, сколько записей не вошло.
  function renderUsd(box, rep) {
    var u = rep.usd;
    if (!rep.count) { box.textContent = ""; return; }
    var miss = u.records_without_usd || 0;
    var note = "нет курса для " + miss + " " + recordsWord(miss);
    if (miss >= rep.count) { box.textContent = "≈ USD: " + note; return; }
    var t = "≈ USD: расходы " + fmtCents(u.out_cents) + ", поступления " + fmtCents(u.in_cents) +
      ", итог " + fmtCents(u.net_cents, true);
    if (miss) { t += ". Не вошло: " + note + "."; }
    box.textContent = t;
  }

  // Строка записи: нажатие на строку раскрывает действия (изменить, удалить / вернуть). Кнопок в списке нет,
  // пока строку не нажали. ctx: {msg: id плашки сообщений, reload: перезагрузка списка}
  function recordRow(r, ctx) {
    if (r.exchange_id) { ctx = null; }                   // комиссия обмена правится только вместе с обменом
    var li = el("li", "rec" + (r.status === "cancelled" || r.closed_by ? " gone" : "") + (ctx ? " tappable" : ""));
    var top = el("div", "rec-top");
    top.appendChild(el("span", "rec-cat", r.category || "без категории"));
    var signed = (r.direction === "in" ? 1 : -1) * r.amount_cents;
    top.appendChild(el("span", "amt " + (signed < 0 ? "neg" : "pos"), fmtCents(signed, true) + " " + r.currency));
    li.appendChild(top);
    var sub = [shortDate(r.occurred_on)];
    if (r.method_title) { sub.push(r.method_title); }
    else if (r.direction === "in" && !r.closed_by) { sub.push("без способа"); }
    if (r.note) { sub.push(r.note); }
    if (r.direction === "in" && r.payer && (r.note || "").indexOf(r.payer) < 0) { sub.push(r.payer); }
    if (r.direction === "in" && r.segment) { sub.push(r.segment.toUpperCase()); }
    if (r.exchange_id) { sub.push("комиссия обмена №" + r.exchange_id); }
    if (r.status === "cancelled") { sub.push("удалена"); }
    if (r.closed_by) { sub.push("перекрыта закрытием: " + monthTitle(r.closed_by).toLowerCase()); }
    li.appendChild(el("div", "rec-sub", sub.join(" · ")));
    if (ctx) {
      li.setAttribute("tabindex", "0");
      var toggle = function () {
        var open = li.querySelector(".acts, .editor");
        if (open) { li.removeChild(open); li.removeAttribute("aria-expanded"); return; }
        li.setAttribute("aria-expanded", "true");
        li.appendChild(actionsFor(r, li, ctx));
      };
      li.addEventListener("click", function (ev) {
        // путь события считается в момент нажатия: кнопка внутри .acts могла уже исчезнуть из дерева
        var inside = ev.composedPath().some(function (n) {
          return n.classList && (n.classList.contains("acts") || n.classList.contains("editor"));
        });
        if (!inside) { toggle(); }
      });
      li.addEventListener("keydown", function (ev) {
        if (ev.target === li && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); toggle(); }
      });
    }
    return li;
  }

  function actionsFor(r, li, ctx) {
    var box = el("div", "acts");
    function btn(text, cls, fn) {
      var b = el("button", "mini " + (cls || ""), text);
      b.type = "button";
      b.addEventListener("click", fn);
      box.appendChild(b);
      return b;
    }
    if (r.status === "cancelled") {
      btn("Вернуть", "", function () { doAction(r.id, "restore", {}, "Запись №" + r.id + " возвращена.", ctx); });
      return box;
    }
    btn("Изменить", "", function () { openEditor(r, li, box, ctx); });
    btn("Удалить", "danger", function () {
      clear(box);
      box.appendChild(el("span", "ask", "Удалить запись №" + r.id + "?"));
      btn("Да, удалить", "danger", function () {
        doAction(r.id, "cancel", {}, "Запись №" + r.id + " удалена.", ctx, r.id);
      });
      btn("Нет", "", function () { li.replaceChild(actionsFor(r, li, ctx), box); });
    });
    return box;
  }

  function doAction(id, action, body, doneText, ctx, undoId) {
    postJson("api/records/" + id + "/" + action, body).then(function (res) {
      if (res.status === 200 && res.body.ok) {
        setMsg(ctx.msg, doneText, false, undoId, ctx);
        ctx.reload();
        return;
      }
      setMsg(ctx.msg, ERRORS[(res.body || {}).error] || "Не получилось. Попробуйте ещё раз.", true);
    }).catch(function () { setMsg(ctx.msg, "Нет связи. Ничего не изменено.", true); });
  }

  // плашка сообщения над списком; после удаления - «↩ Вернуть» (в «Сегодня» удалённая запись из списка уходит)
  function setMsg(id, text, isErr, undoId, ctx) {
    var m = $(id);
    m.className = isErr ? "err" : "okbox";
    m.textContent = text;
    if (undoId && ctx) {
      var b = el("button", "mini", "↩ Вернуть");
      b.type = "button";
      b.addEventListener("click", function () {
        doAction(undoId, "restore", {}, "Запись №" + undoId + " возвращена.", ctx);
      });
      m.appendChild(document.createTextNode(" "));
      m.appendChild(b);
    }
    show(m, !!text);
  }

  function centsToInput(c) {
    var a = Math.abs(c), frac = a % 100;
    return String(Math.floor(a / 100)) + (frac ? "," + ("0" + frac).slice(-2) : "");
  }

  function selectOf(options, value) {
    var s = document.createElement("select");
    options.forEach(function (o) {
      var op = el("option", "", o.label); op.value = o.value; s.appendChild(op);
    });
    s.value = value;
    return s;
  }

  function field(label, node) {
    var wrap = el("div", "edit-field");
    wrap.appendChild(el("div", "hint", label));
    wrap.appendChild(node);
    return wrap;
  }

  function openEditor(r, li, actsBox, ctx) {
    var meta = state.meta, isIn = r.direction === "in";
    var ed = el("div", "editor");
    var err = el("p", "err"); err.hidden = true;
    var warn = el("p", "warn"); warn.hidden = true;
    ed.appendChild(err); ed.appendChild(warn);

    var amount = document.createElement("input");
    amount.type = "text"; amount.setAttribute("inputmode", "decimal"); amount.maxLength = 20;
    amount.value = centsToInput(r.amount_cents);
    var amount0 = amount.value;
    ed.appendChild(field("Сумма", amount));

    var codes = meta.currencies.main.concat(meta.currencies.second || []);
    state.catalog.forEach(function (c) { if (codes.indexOf(c.code) < 0) { codes.push(c.code); } });
    if (codes.indexOf(r.currency) < 0) { codes.push(r.currency); }
    var names = {};
    state.catalog.forEach(function (c) { names[c.code] = c.name; });
    var curSel = selectOf(codes.map(function (c) { return { value: c, label: c + (names[c] ? " · " + names[c] : "") }; }),
                          r.currency);
    ed.appendChild(field("Валюта", curSel));

    var note = document.createElement("input");
    note.type = "text"; note.maxLength = 200; note.value = r.note || "";
    ed.appendChild(field("На что", note));

    var cats = meta.categories[r.direction].slice();
    if (r.category && cats.indexOf(r.category) < 0) { cats.push(r.category); }
    var catOpts = cats.map(function (c) { return { value: c, label: c }; });
    if (!isIn) { catOpts.push({ value: "__other", label: "Другая…" }); }
    var catSel = selectOf(catOpts, r.category || cats[0]);
    var catOther = document.createElement("input");
    catOther.type = "text"; catOther.maxLength = 40; catOther.placeholder = "Название категории"; catOther.hidden = true;
    catSel.addEventListener("change", function () { catOther.hidden = catSel.value !== "__other"; });
    var catWrap = field("Категория", catSel); catWrap.appendChild(catOther);
    ed.appendChild(catWrap);

    // у старых записей без способа (импорт) «Без способа» остаётся, пока способ не выберут; у новых его нет совсем
    var mOpts = r.method ? [] : [{ value: "", label: "Без способа" }];
    meta.methods.filter(function (m) { return m.scope === r.direction || m.scope === "both"; })
      .forEach(function (m) { mOpts.push({ value: m.code, label: m.title }); });
    if (r.method && !mOpts.some(function (o) { return o.value === r.method; })) {
      mOpts.push({ value: r.method, label: methodTitle(r.method) });
    }
    mOpts.push({ value: "__other", label: "Другой…" });
    var methSel = selectOf(mOpts, r.method || "");
    var methOther = document.createElement("input");
    methOther.type = "text"; methOther.maxLength = 30; methOther.placeholder = "Название способа"; methOther.hidden = true;
    methSel.addEventListener("change", function () { methOther.hidden = methSel.value !== "__other"; });
    var methWrap = field("Способ", methSel);
    methWrap.appendChild(methOther);
    ed.appendChild(methWrap);

    // плательщик (только у поступления): метки из payers.json и «Другой» (свой текст до 30 знаков); повторное нажатие
    // снимает выбор. Выбор плательщика ставит его сегмент по умолчанию, пока сегмент не нажимали руками.
    var payers = meta.payers || [];
    var payerState = r.payer ? (payers.some(function (p) { return p.label === r.payer; }) ? r.payer : "__other") : "";
    var payerBox = el("div", "chips");
    var payerOther = document.createElement("input");
    payerOther.type = "text"; payerOther.maxLength = 30; payerOther.placeholder = "Кто платил";
    payerOther.value = payerState === "__other" ? r.payer : "";
    payerOther.hidden = payerState !== "__other";
    var segState = r.segment || "", segTouched = false;
    var segBox = el("div", "chips");
    function paintSeg() {
      chips(segBox, SEGMENT_ITEMS, segState, function (v) {
        segState = segState === v ? "" : v; segTouched = true; paintSeg(); touchedEditor();
      });
    }
    function paintPayer() {
      var items = payers.map(function (p) { return { value: p.label, label: p.label }; });
      items.push({ value: "__other", label: "Другой" });
      chips(payerBox, items, payerState, function (v) {
        payerState = payerState === v ? "" : v;
        var chosen = payers.filter(function (p) { return p.label === payerState; })[0];
        if (chosen && chosen.segment && !segTouched) { segState = chosen.segment; paintSeg(); }
        paintPayer(); touchedEditor();
      });
      payerOther.hidden = payerState !== "__other";
    }
    if (isIn) {
      paintPayer();
      var payerWrap = field("От кого", payerBox);
      payerWrap.appendChild(payerOther);
      ed.appendChild(payerWrap);
      paintSeg(); ed.appendChild(field("Сегмент", segBox));
    }

    var pending = [];
    var row = el("div", "acts");
    var save = el("button", "mini primary", "Сохранить");
    save.type = "button";
    var cancel = el("button", "mini", "Отмена");
    cancel.type = "button";
    row.appendChild(save); row.appendChild(cancel);
    ed.appendChild(row);

    function showErr(code) { err.textContent = ERRORS[code] || "Не получилось. Проверьте поля."; err.hidden = false; }
    function touchedEditor() { err.hidden = true; warn.hidden = true; pending = []; save.textContent = "Сохранить"; }
    [amount, curSel, note, catSel, catOther, methSel, methOther, payerOther].forEach(function (n) {
      n.addEventListener("input", touchedEditor); n.addEventListener("change", touchedEditor);
    });

    save.addEventListener("click", function () {
      var body = {};
      var amountChanged = amount.value.trim() !== amount0;
      var bad = amountChanged ? amountError(amount.value) : null;
      if (bad) { showErr(bad); return; }
      if (amountChanged) { body.amount = amount.value.trim(); }
      if (curSel.value !== r.currency) { body.currency = curSel.value; }
      if (note.value.trim() !== (r.note || "")) { body.note = note.value.trim(); }
      var cat = catSel.value === "__other" ? catOther.value.trim() : catSel.value;
      if (catSel.value === "__other" && !cat) { showErr("missing_category"); return; }
      if (cat !== r.category) { body.category = cat; }
      if (methSel.value === "__other") {
        if (!methOther.value.trim()) { showErr("method_required"); return; }
        body.new_method = methOther.value.trim();
      } else if (methSel.value !== (r.method || "")) {
        body.method = methSel.value;
      }
      if (isIn) {
        var payerNew = payerState === "__other" ? payerOther.value.trim().replace(/\s+/g, " ") : payerState;
        if (payerNew !== (r.payer || "")) { body.payer = payerNew || null; }
      }
      if (isIn && segState !== (r.segment || "")) { body.segment = segState || null; }
      if (!Object.keys(body).length) { ctx.reload(); return; }
      if (pending.length) { body.confirm = pending.slice(); }
      save.disabled = true;
      postJson("api/records/" + r.id + "/update", body).then(function (res) {
        save.disabled = false;
        var b = res.body || {};
        if (res.status === 200 && b.ok) {
          setMsg(ctx.msg, "Запись №" + r.id + " изменена.", false);
          ctx.reload();
          return;
        }
        if (res.status === 409 && b.error === "confirm_required") {
          pending = (b.warnings || []).slice();
          warn.textContent = WARN_TEXT.outlier;
          warn.hidden = false;
          save.textContent = "Да, столько";
          return;
        }
        showErr(res.status === 401 || res.status === 403 ? "server_error" : b.error);
      }).catch(function () { save.disabled = false; err.textContent = "Нет связи. Ничего не изменено."; err.hidden = false; });
    });
    cancel.addEventListener("click", function () { li.removeChild(ed); li.appendChild(actionsFor(r, li, ctx)); });
    li.replaceChild(ed, actsBox);
  }

  var CTX_TODAY = { msg: "today-msg", reload: function () { loadToday(); } };
  var CTX_RECENT = { msg: "recent-msg", reload: function () { loadRecent(); } };

  // --- диаграммы (этап 7): горизонтальные полосы и столбцы по месяцам; только CSS и inline SVG ------------------
  // Полосы: группы {name, usd_cents, without_usd, count} с сервера (расходы по категориям, поступления по способам),
  // длина полосы пропорциональна сумме в USD; подпись и сумма (та же fmtCents, что в строке «≈ USD») над полосой.
  function barChart(box, groups, kind, withDetails) {
    clear(box);
    var sorted = groups.slice().sort(function (a, b) {
      return b.usd_cents - a.usd_cents || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
    });
    var max = 0;
    sorted.forEach(function (g) { if (g.usd_cents > max) { max = g.usd_cents; } });
    sorted.forEach(function (g) {
      var row = el("div", "bar-row" + (withDetails ? " bar-tap" : ""));
      var head = el("div", "bar-head");
      head.appendChild(el("span", "bar-name", g.name));
      var noRate = g.without_usd >= g.count;
      var val = el("span", "bar-val", noRate ? "нет курса" : "≈ " + fmtCents(g.usd_cents));
      val.setAttribute("data-cents", String(noRate ? 0 : g.usd_cents));
      head.appendChild(val);
      row.appendChild(head);
      var track = el("div", "bar-track");
      var fill = el("div", "bar-fill" + (kind === "in" ? " in" : ""));
      var pct = max > 0 && g.usd_cents > 0 ? Math.max(1.5, Math.round(g.usd_cents / max * 1000) / 10) : 0;
      fill.style.width = pct + "%";               // через CSSOM: политика CSP запрещает style="" в разметке
      track.appendChild(fill);
      row.appendChild(track);
      if (g.without_usd > 0 && !noRate) {
        row.appendChild(el("div", "bar-note", "не вошло: нет курса для " + g.without_usd + " " + recordsWord(g.without_usd)));
      }
      if (withDetails) { addDetails(row, g); }
      box.appendChild(row);
    });
  }

  // нажатие на полосу раскрывает под ней число записей и суммы по валютам записей; повторное нажатие закрывает
  function addDetails(row, g) {
    row.setAttribute("tabindex", "0");
    row.setAttribute("aria-expanded", "false");
    var toggle = function () {
      var open = row.querySelector(".bar-detail");
      if (open) { row.removeChild(open); row.setAttribute("aria-expanded", "false"); return; }
      var parts = g.currencies.map(function (c) { return fmtCents(c.cents) + " " + c.currency; });
      row.appendChild(el("div", "bar-detail", g.count + " зап. · " + parts.join(", ")));
      row.setAttribute("aria-expanded", "true");
    };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", function (ev) {
      if (ev.target === row && (ev.key === "Enter" || ev.key === " ")) { ev.preventDefault(); toggle(); }
    });
  }

  // месяц: секция есть всегда, при отсутствии данных - «нет»
  function monthBars(box, groups, kind, emptyText) {
    if (!groups.length) { clear(box); box.appendChild(el("div", "empty", emptyText)); return; }
    barChart(box, groups, kind, true);
  }

  // minGroups: сколько групп нужно, чтобы диаграмма вообще появилась («Сегодня»: от 3 категорий)
  function showBars(section, box, groups, kind, minGroups) {
    var on = groups.length >= (minGroups || 1);
    show(section, on);
    if (on) { barChart(box, groups, kind); } else { clear(box); }
  }

  var SVGNS = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs, text) {
    var e = document.createElementNS(SVGNS, tag);
    Object.keys(attrs || {}).forEach(function (k) { e.setAttribute(k, attrs[k]); });
    if (text !== undefined) { e.textContent = text; }
    return e;
  }
  var MONTH_SHORT = ["янв", "фев", "мар", "апр", "май", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];

  // короткая подпись суммы над столбцом: 850, 3,4k, 12k (доллары, центы не нужны)
  function compactUsd(cents, signed) {
    var neg = cents < 0, d = Math.round(Math.abs(cents) / 100), s;
    if (d >= 10000) { s = Math.round(d / 1000) + "k"; }
    else if (d >= 1000) { s = String(Math.round(d / 100) / 10).replace(".", ",") + "k"; }
    else { s = String(d); }
    return (neg ? "−" : (signed ? "+" : "")) + s;
  }

  // Динамика по месяцам: по каждому месяцу столбцы «поступления» и «расходы», под ним нетто; закрытый месяц с галочкой.
  var TREND_W = 320, TREND_PLOT = 110, TREND_TOP = 16;
  function renderTrend(box, hint, months, current) {
    clear(box); clear(hint);
    var n = months.length, max = 0;
    months.forEach(function (m) { max = Math.max(max, m.in_usd_cents, m.out_usd_cents); });
    if (!n || max <= 0) { box.appendChild(el("p", "note", "Данных пока нет")); return; }
    var slot = TREND_W / n, barW = Math.min(26, Math.floor((slot - 6) / 2));
    var base = TREND_TOP + TREND_PLOT, height = base + 52;
    var svg = svgEl("svg", { viewBox: "0 0 " + TREND_W + " " + height, role: "img",
                             "aria-label": "Динамика по месяцам: поступления, расходы и нетто в долларах" });
    var partial = false, anyClosed = false;
    months.forEach(function (m, i) {
      var x0 = i * slot, cx = x0 + slot / 2;
      if (m.month === current) {
        svg.appendChild(svgEl("rect", { "class": "t-bg", x: x0 + 1, y: 0, width: slot - 2, height: height, rx: 6 }));
      }
      [["t-in", m.in_usd_cents, cx - barW - 1], ["t-out", m.out_usd_cents, cx + 1]].forEach(function (b) {
        if (b[1] <= 0) { return; }
        var h = Math.max(1.5, b[1] / max * TREND_PLOT);
        var r = svgEl("rect", { "class": b[0], x: b[2], y: base - h, width: barW, height: h, rx: 2 });
        r.setAttribute("data-cents", String(b[1]));
        svg.appendChild(r);
        svg.appendChild(svgEl("text", { x: b[2] + barW / 2, y: base - h - 3, "text-anchor": "middle" }, compactUsd(b[1])));
      });
      var star = m.records_without_usd > 0;
      if (star) { partial = true; }
      var label = svgEl("text", { "class": "t-month" + (m.month === current ? " t-sel" : ""), x: cx, y: base + 14,
                                  "text-anchor": "middle" }, MONTH_SHORT[+m.month.slice(5, 7) - 1] + (star ? "*" : ""));
      label.setAttribute("data-month", m.month);
      svg.appendChild(label);
      var net = svgEl("text", { "class": "t-net" + (m.net_usd_cents < 0 ? " t-neg" : ""), x: cx, y: base + 30,
                                "text-anchor": "middle" }, compactUsd(m.net_usd_cents, true));
      net.setAttribute("data-cents", String(m.net_usd_cents));
      svg.appendChild(net);
      if (m.closed) {
        anyClosed = true;
        svg.appendChild(svgEl("path", { "class": "t-check", d: "M" + (cx - 4) + " " + (base + 43) + "l3 3l5 -6" }));
      }
    });
    svg.appendChild(svgEl("line", { "class": "t-axis", x1: 0, x2: TREND_W, y1: base + 0.5, y2: base + 0.5 }));
    box.appendChild(svg);
    hint.appendChild(el("span", "key in"));
    hint.appendChild(document.createTextNode("поступления "));
    hint.appendChild(el("span", "key out"));
    hint.appendChild(document.createTextNode("расходы. Под месяцем нетто"));
    if (anyClosed) { hint.appendChild(document.createTextNode(", ✓ месяц закрыт")); }
    if (partial) { hint.appendChild(document.createTextNode(". * у части записей нет курса, сумма неполная")); }
    hint.appendChild(document.createTextNode("."));
  }

  function loadTrend() {
    var box = $("month-trend"), hint = $("month-trend-hint");
    return api("api/report?period=trend").then(function (res) {
      if (res.status !== 200 || !res.body.months) { show($("month-trend-box"), false); return; }
      show($("month-trend-box"), true);
      renderTrend(box, hint, res.body.months, state.month);
    }).catch(function () { show($("month-trend-box"), false); });
  }

  function loadToday() {
    var box = $("today-totals");
    return api("api/report?period=day").then(function (res) {
      if (authFail(res.status, box)) { clear($("today-list")); $("today-usd").textContent = ""; show($("today-chart"), false); return; }
      var rep = res.body;
      $("today-title").textContent = "Сегодня, " + dayTitle(rep.from);
      renderTotals(box, rep);
      renderUsd($("today-usd"), rep);
      setPeriodCount($("today-ex"), rep.exchanges || 0);
      showBars($("today-chart"), $("today-bars"), rep.by_category || [], "out", 3);      // диаграмма от 3 категорий
      var list = $("today-list");
      clear(list);
      rep.records.forEach(function (r) { list.appendChild(recordRow(r, CTX_TODAY)); });
    }).catch(function () { box.textContent = "Нет связи. Попробуйте ещё раз."; });
  }

  function fmtRate(v) { return String(Math.round(v * 10000) / 10000).replace(".", ","); }

  // Итог месяца крупно: поступления, расходы, нетто в USD. Без курса не врёт нулями (как строка «≈ USD» в «Сегодня»).
  function renderMonthSummary(box, rep) {
    clear(box);
    if (!rep.count) { box.appendChild(el("div", "sum-title", "≈ USD")); box.appendChild(el("div", "note", "Записей нет")); return; }
    var u = rep.usd, miss = u.records_without_usd || 0;
    if (miss >= rep.count) {
      box.appendChild(el("div", "sum-title", "≈ USD: нет курса для " + miss + " " + recordsWord(miss)));
      return;
    }
    box.appendChild(el("div", "sum-title", "≈ USD"));
    [["Поступления", u.in_cents, "in", false], ["Расходы", u.out_cents, "out", false], ["Нетто", u.net_cents, "net", true]]
      .forEach(function (r) {
        var line = el("div", "sum-row" + (r[2] === "net" ? " net" : ""));
        line.appendChild(el("span", "", r[0]));
        var v = el("span", "amt", fmtCents(r[1], r[3]));
        v.setAttribute("data-k", r[2]); v.setAttribute("data-cents", String(r[1]));
        line.appendChild(v);
        box.appendChild(line);
      });
    if (miss) { box.appendChild(el("div", "sum-note", "Не вошло: нет курса для " + miss + " " + recordsWord(miss) + ".")); }
  }

  // закрытый месяц: итоги заданы закрытием, курсы - курсы закрытия, не текущие; курсы свёрнуты
  function renderClosed(closed) {
    var box = $("month-closed"), rates = $("month-rates"), tg_ = $("month-rates-toggle");
    state.showRates = false;
    tg_.setAttribute("aria-expanded", "false"); tg_.textContent = "Курсы ▾";
    if (!closed) { show(box, false); show(rates, false); return; }
    $("month-closed-text").textContent = closed.matches ? "Месяц закрыт. Итоги совпадают с закрытием."
      : "Месяц закрыт, но итоги не совпадают с закрытием. Сообщите об этом.";
    box.className = closed.matches ? "hint" : "bad";
    show(box, true);
    var keys = Object.keys(closed.rates || {}).sort();
    rates.textContent = keys.length ? "Курсы закрытия: " + keys.map(function (k) {
      return k.replace(/_per_USD$/, "").replace(/_usd$/i, "").replace(/_/g, "/") + " " + fmtRate(closed.rates[k]);
    }).join(" · ") : "";
    show(tg_, keys.length > 0);
    show(rates, false);
  }

  function loadMonth() {
    var box = $("month-usd");              // сообщения об ошибках - в карточке итога (блок по валютам свёрнут)
    if (!state.month) { state.month = state.meta.today.slice(0, 7); }
    return api("api/report?period=month&month=" + state.month).then(function (res) {
      if (authFail(res.status, box)) { return; }
      var rep = res.body;
      $("month-title").textContent = monthTitle(state.month);
      $("month-next").disabled = state.month >= state.meta.today.slice(0, 7);
      renderTotals($("month-totals"), rep);
      renderMonthSummary(box, rep);
      setPeriodCount($("month-ex"), rep.exchanges || 0);
      renderClosed(rep.closed);
      monthBars($("month-cats-bars"), rep.by_category, "out", "Расходов нет");
      monthBars($("month-methods-bars"), rep.by_method, "in", "Поступлений нет");
      // сегменты: у закрытого месяца их нет (итог задан закрытием), секцию не показываем
      var segs = rep.by_segment || [];
      show($("month-segments-chart"), !rep.closed && segs.length > 0);
      if (!rep.closed && segs.length) { monthBars($("month-segments-bars"), segs, "in", "Поступлений нет"); }
      else { clear($("month-segments-bars")); }
      loadTrend();
    }).catch(function () { box.textContent = "Нет связи. Попробуйте ещё раз."; });
  }

  // --- отчёт за период (api/report?period=range&from=&to=): тот же вид, что «Месяц» ---
  function addDays(iso, delta) {
    var d = new Date(iso + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + delta);
    return d.toISOString().slice(0, 10);
  }
  function periodTitle(from, to) {
    return from === to ? dayTitle(from) : dayTitle(from) + " - " + dayTitle(to);
  }
  function setPeriod(from, to) {
    $("period-from").value = from; $("period-to").value = to;
    loadPeriod();
  }
  function loadPeriod() {
    var box = $("period-usd"), msg = $("period-msg"), today = state.meta.today;
    if (!$("period-from").value || !$("period-to").value) {
      var t = today;
      $("period-to").value = t; $("period-from").value = addDays(t, -6);
    }
    var from = $("period-from").value, to = $("period-to").value;
    $("period-from").max = today; $("period-to").max = today;
    return api("api/report?period=range&from=" + from + "&to=" + to).then(function (res) {
      if (res.status === 422) {
        show(msg, true); msg.textContent = ERRORS[res.body.error] || ERRORS.bad_period;
        clear(box); clear($("period-totals")); clear($("period-cats-bars")); clear($("period-methods-bars"));
        $("period-title").textContent = "Период";
        return;
      }
      show(msg, false);
      if (authFail(res.status, box)) { return; }
      var rep = res.body;
      $("period-title").textContent = periodTitle(rep.from, rep.to);
      renderTotals($("period-totals"), rep);
      renderMonthSummary(box, rep);
      setPeriodCount($("period-ex"), rep.exchanges || 0);
      monthBars($("period-cats-bars"), rep.by_category, "out", "Расходов нет");
      monthBars($("period-methods-bars"), rep.by_method, "in", "Поступлений нет");
      var segs = rep.by_segment || [];
      show($("period-segments-chart"), segs.length > 0);
      if (segs.length) { monthBars($("period-segments-bars"), segs, "in", "Поступлений нет"); }
      else { clear($("period-segments-bars")); }
    }).catch(function () { box.textContent = "Нет связи. Попробуйте ещё раз."; });
  }

  function loadRecent() {
    var list = $("recent-list");
    return api("api/recent" + (state.recentFilter ? "?filter=" + state.recentFilter : "")).then(function (res) {
      if (res.status !== 200) {
        clear(list);
        list.appendChild(el("li", "rec empty", res.status === 401 ? "Откройте Кассу заново." :
          res.status === 403 ? "Нет доступа." : "Не удалось загрузить. Попробуйте ещё раз."));
        return;
      }
      var n = res.body.no_method_total;
      var items = [{ value: "", label: "Все" }];
      if (n > 0 || state.recentFilter === "no_method") { items.push({ value: "no_method", label: "Без способа (" + n + ")" }); }
      var nc = res.body.closed_total || 0;
      if (nc > 0 || state.recentFilter === "closed") { items.push({ value: "closed", label: "Закрытые месяцы (" + nc + ")" }); }
      chips($("recent-filter"), items, state.recentFilter, function (v) {
        state.recentFilter = v; setMsg("recent-msg", "", false); loadRecent();
      });
      clear(list);
      if (!res.body.records.length) { list.appendChild(el("li", "rec empty", "Пока пусто")); }
      res.body.records.forEach(function (r) { list.appendChild(recordRow(r, r.closed_by ? null : CTX_RECENT)); });
    }).catch(function () {
      clear(list);
      list.appendChild(el("li", "rec empty", "Нет связи. Попробуйте ещё раз."));
    });
  }

  // --- «Входящие»: черновики из чата. Запись только отсюда, с подписью Telegram ---------
  function setInboxCount(n) { $("inbox-count").textContent = n > 0 ? " " + n : ""; }

  function draftRow(d) {
    var li = el("li", "rec");
    var top = el("div", "rec-top");
    top.appendChild(el("span", "rec-cat", (d.source === "voice" ? "🎙 " : "") + (d.heard || "без текста")));
    li.appendChild(top);
    var sub = d.kind === "exchange" ? [d.summary, d.date_text]
      : [(d.direction === "in" ? "Поступление" : "Расход") + " " + d.amount.replace(".", ",") + " " + d.currency,
         d.category, d.date_text];
    if (d.kind !== "exchange") { sub.push(d.method_title ? d.method_title : "способ не выбран"); }
    li.appendChild(el("div", "rec-sub", sub.join(" · ")));
    var acts = el("div", "acts");
    var open = el("button", "mini primary", "Проверить и записать");
    open.type = "button";
    open.addEventListener("click", function () { showTab("record"); applyDraft(d.id, false); });
    var del = el("button", "mini danger", "Удалить");
    del.type = "button";
    del.addEventListener("click", function () {
      postJson("api/draft/cancel", { id: d.id }).then(function (res) {
        setMsg("inbox-msg", res.status === 200 ? "Черновик удалён, ничего не записано." :
          (ERRORS[(res.body || {}).error] || "Не получилось."), res.status !== 200);
        loadInbox(true);
      }).catch(function () { setMsg("inbox-msg", "Нет связи. Ничего не изменено.", true); });
    });
    acts.appendChild(open); acts.appendChild(del);
    li.appendChild(acts);
    return li;
  }

  function loadInbox(quiet) {
    var list = $("inbox-list"), bb = $("inbox-bybit");
    return api("api/inbox").then(function (res) {
      if (res.status !== 200) {
        if (!quiet) {
          clear(list); clear(bb);
          list.appendChild(el("li", "rec empty", res.status === 401 ? "Откройте Кассу заново." :
            res.status === 403 ? "Нет доступа." : "Не удалось загрузить. Попробуйте ещё раз."));
        }
        return;
      }
      var b = res.body;
      setInboxCount(b.drafts.length);
      clear(list); clear(bb);
      if (!b.drafts.length) { list.appendChild(el("li", "rec empty", "Пока пусто")); }
      b.drafts.forEach(function (d) { list.appendChild(draftRow(d)); });
    }).catch(function () {
      if (!quiet) { clear(list); list.appendChild(el("li", "rec empty", "Нет связи. Попробуйте ещё раз.")); }
    });
  }

  // --- обмен валют (схема 7): режим «Обмен» формы и вкладка «Обмены». Запись только с подписью (как «Записать») ---------
  var X_ERRORS = {
    bad_give: "Проверьте сумму и валюту отданного.",
    bad_get: "Добавьте, что получили: сумма и валюта.",
    too_many_legs: "Получить можно не больше трёх валют.",
    duplicate_get_currency: "Одна и та же валюта получения указана дважды.",
    same_currency: "Отдали и получили одну валюту: это не обмен.",
    bad_part: "Доля отданного: только цифры и не больше двух знаков после запятой.",
    part_too_large: "Доли больше, чем отдано.",
    parts_mismatch: "Доли не сходятся с отданной суммой: уберите лишнюю или оставьте одну долю пустой.",
    bad_fee: "Комиссия: сумма цифрами.",
    draft_not_exchange: "Эта карточка не про обмен.",
    draft_is_exchange: "Эта карточка про обмен: откройте её в режиме «Обмен».",
    linked_to_exchange: "Это комиссия обмена: меняйте её в обмене (вкладка «Обмены»).",
    exchange_cancelled: "Обмен удалён. Сначала верните его.",
    bad_draft: "Не удалось открыть карточку."
  };
  Object.keys(X_ERRORS).forEach(function (k) { ERRORS[k] = X_ERRORS[k]; });

  function freshExchange() {
    var m = state.methodBy && state.methodBy.out;
    return { giveAmount: "", giveCur: "RUB", giveMethod: m || "",
             legs: [{ amount: "", cur: "TRY", method: "", part: "" }],
             feeAmount: "", feeCur: "", note: "", dateKey: "today", datePick: "", editId: null, clientId: uuid() };
  }

  function curCodes() {
    var c = state.meta.currencies, codes = c.main.concat(c.second || []).concat(c.recent || []);
    state.catalog.forEach(function (x) { if (codes.indexOf(x.code) < 0) { codes.push(x.code); } });
    return codes;
  }
  function fillCurSelect(sel, value, keep) {
    var names = {};
    state.catalog.forEach(function (c) { names[c.code] = c.name; });
    var codes = curCodes();
    if (codes.indexOf(value) < 0) { codes.push(value); }
    clear(sel);
    codes.forEach(function (c) {
      var o = el("option", "", c + (names[c] ? " · " + names[c] : "")); o.value = c; sel.appendChild(o);
    });
    sel.value = value;
  }
  function fillMethodSelect(sel, scope, value) {
    clear(sel);
    var ph = el("option", "", "Способ не указан"); ph.value = ""; sel.appendChild(ph);
    state.meta.methods.filter(function (m) { return m.scope === scope || m.scope === "both"; })
      .forEach(function (m) { var o = el("option", "", m.title); o.value = m.code; sel.appendChild(o); });
    sel.value = value;
  }

  function exDate() {
    var x = state.x, today = state.meta.today;
    return x.dateKey === "today" ? today : x.dateKey === "yesterday" ? shiftDate(today, -1) : x.datePick;
  }
  function exDateClosed() {
    var d = state.x && state.meta ? exDate() : "";
    return !!d && (state.meta.closed_months || []).indexOf(d.slice(0, 7)) >= 0;
  }

  // сумма из поля -> центы (целое) или null; те же правила, что у формы записи
  function toCents(text) {
    var raw = String(text).replace(/[\s  ]/g, "");
    if (!raw || amountError(raw)) { return null; }
    var p = raw.replace(",", ".").split(".");
    return +p[0] * 100 + (p[1] ? +(p[1] + "0").slice(0, 2) : 0);
  }

  // живой расчёт курса: отдано за единицу полученного. Неуказанные доли при нескольких валютах оценит касса при записи
  function exRateLines() {
    var x = state.x, give = toCents(x.giveAmount), out = [], unknown = false;
    if (give === null) { return ""; }
    x.legs.forEach(function (l) {
      var get = toCents(l.amount), part = l.part ? toCents(l.part) : null;
      if (get === null) { return; }
      if (x.legs.length === 1) { part = give; }
      if (part === null) { unknown = true; return; }
      out.push("1 " + l.cur + " = " + fmtRate(part / get) + " " + x.giveCur);
    });
    if (unknown) { out.push("Курс остальных касса оценит при записи: доли не указаны, делит по рыночной стоимости."); }
    return out.join("\n");
  }

  function exRefresh() {
    $("x-rate").textContent = exRateLines();
    var closed = exDateClosed();
    $("x-date-hint").textContent = closed ? ERRORS.month_closed : "";
    show($("x-date-hint"), closed);
    var needMethod = !!state.x.feeAmount.trim() && !state.x.giveMethod;
    show($("x-fee-hint"), needMethod);
    refreshButton();
  }
  function exTouched() {
    setText($("err"), ""); setText($("warn"), ""); show($("ok"), false);
    exRefresh();
  }

  function renderLegs() {
    var x = state.x, box = $("x-legs");
    clear(box);
    x.legs.forEach(function (leg, i) {
      var row = el("div", "xleg");
      var top = el("div", "xrow");
      var amt = document.createElement("input");
      amt.type = "text"; amt.className = "x-leg-amount"; amt.maxLength = 20; amt.placeholder = "Получил";
      amt.setAttribute("inputmode", "decimal"); amt.value = leg.amount;
      amt.addEventListener("input", function () { leg.amount = amt.value; exTouched(); });
      var cur = document.createElement("select");
      cur.className = "x-leg-cur"; cur.setAttribute("aria-label", "Валюта получения");
      fillCurSelect(cur, leg.cur);
      cur.addEventListener("change", function () { leg.cur = cur.value; exTouched(); });
      top.appendChild(amt); top.appendChild(cur);
      row.appendChild(top);
      var meth = document.createElement("select");
      meth.className = "x-leg-method"; meth.setAttribute("aria-label", "Способ получения");
      fillMethodSelect(meth, "in", leg.method);
      meth.addEventListener("change", function () { leg.method = meth.value; exTouched(); });
      row.appendChild(meth);
      if (x.legs.length > 1) {
        var part = document.createElement("input");
        part.type = "text"; part.className = "x-leg-part"; part.maxLength = 20;
        part.setAttribute("inputmode", "decimal"); part.placeholder = "Сколько отдано за неё (необязательно)";
        part.value = leg.part;
        part.addEventListener("input", function () { leg.part = part.value; exTouched(); });
        row.appendChild(part);
        var del = el("button", "mini x-leg-del", "Убрать");
        del.type = "button";
        del.addEventListener("click", function () { x.legs.splice(i, 1); renderLegs(); exTouched(); });
        row.appendChild(del);
      }
      box.appendChild(row);
    });
    show($("x-add-leg"), x.legs.length < 3);
  }

  function renderExchangeForm() {
    var x = state.x;
    $("x-give-amount").value = x.giveAmount;
    fillCurSelect($("x-give-cur"), x.giveCur);
    fillMethodSelect($("x-give-method"), "out", x.giveMethod);
    renderLegs();
    $("x-fee-amount").value = x.feeAmount;
    fillCurSelect($("x-fee-cur"), x.feeCur || x.giveCur);
    $("x-note").value = x.note;
    var dItems = [{ value: "today", label: "Сегодня" }, { value: "yesterday", label: "Вчера" },
                  { value: "pick", label: "Другая дата" }];
    chips($("x-date"), dItems, x.dateKey, function (v) {
      x.dateKey = v; renderExchangeForm(); exTouched();
      if (v === "pick") { try { if ($("x-date-pick").showPicker) { $("x-date-pick").showPicker(); } } catch (e) { /* поле видно */ } }
    });
    show($("x-date-pick"), x.dateKey === "pick");
    $("x-date-pick").max = state.meta.today;
    $("x-date-pick").min = shiftDate(state.meta.today, -366);
    $("x-date-pick").value = x.datePick;
    var d = x.dateKey === "pick" && !x.datePick ? "" : exDate();
    $("x-date-label").textContent = d ? "Дата: " + dayTitle(d) : "Дата";
    show($("x-banner"), !!x.editId || !!state.draftId);
    $("x-banner").textContent = x.editId ? "Правка обмена №" + x.editId + ". Проверьте значения и нажмите «Сохранить»."
      : "Черновик из чата. Проверьте значения и нажмите «Записать»: без этого ничего не запишется.";
    exRefresh();
  }

  function exPayload(forEdit) {
    var x = state.x;
    var give = { amount: x.giveAmount.trim(), currency: x.giveCur };
    if (x.giveMethod) { give.method = x.giveMethod; }
    var p = { date: exDate(), give: give, note: x.note.trim(),
              get: x.legs.map(function (l) {
                var g = { amount: l.amount.trim(), currency: l.cur };
                if (l.method) { g.method = l.method; }
                if (x.legs.length > 1 && l.part.trim()) { g.part = l.part.trim(); }
                return g;
              }),
              fee: x.feeAmount.trim() ? { amount: x.feeAmount.trim(), currency: x.feeCur || x.giveCur } : null };
    if (!forEdit) {
      p.client_id = x.clientId;
      if (state.draftId) { p.draft_id = state.draftId; }
    }
    return p;
  }

  function exValidate() {
    var x = state.x, seen = {};
    if (amountError(x.giveAmount)) { return "bad_give"; }
    for (var i = 0; i < x.legs.length; i++) {
      var l = x.legs[i];
      if (amountError(l.amount)) { return "bad_get"; }
      if (l.part.trim() && amountError(l.part)) { return "bad_part"; }
      if (l.cur === x.giveCur) { return "same_currency"; }
      if (seen[l.cur]) { return "duplicate_get_currency"; }
      seen[l.cur] = true;
    }
    if (x.feeAmount.trim() && amountError(x.feeAmount)) { return "bad_fee"; }
    if (x.feeAmount.trim() && !x.giveMethod) { return "method_required"; }
    if (x.dateKey === "pick" && !x.datePick) { return "bad_date"; }
    if (exDateClosed()) { return "month_closed"; }
    return null;
  }

  function resetExchange() {
    state.x = freshExchange();
    state.draftId = null;
    renderExchangeForm();
    setButton("Записать");
  }

  function submitExchange() {
    if (state.busy) { return; }
    setText($("err"), ""); show($("ok"), false);
    var bad = exValidate();
    if (bad) { setText($("err"), ERRORS[bad] || "Проверьте поля."); haptic("error"); return; }
    var editId = state.x.editId;
    busy(true);
    var call = editId
      ? api("api/exchanges/" + editId, { method: "PATCH", headers: { "Content-Type": "application/json" },
                                         body: JSON.stringify(exPayload(true)) })
      : postJson("api/exchanges", exPayload(false));
    call.then(function (res) {
      busy(false);
      var b = res.body || {};
      if (res.status === 200 && b.ok) {
        haptic("success");
        if (editId) {
          resetExchange();
          showTab("exchanges");
          setMsg("exchanges-msg", "Обмен №" + editId + " изменён.", false);
          return;
        }
        showOk((b.duplicate_request ? "Обмен уже записан №" : "Обмен записан №") + b.id,
               b.duplicate_request ? null : b.id);
        state.lastKind = "ex";
        resetExchange();
        loadMeta(true); loadInbox(true);
        return;
      }
      if (res.status === 401) { setText($("err"), "Откройте Кассу заново."); return; }
      if (res.status === 403) { setText($("err"), "Нет доступа."); return; }
      if (b.error === "idem_conflict") { state.x.clientId = uuid(); }
      setText($("err"), ERRORS[b.error] || "Не записано. Проверьте поля и попробуйте ещё раз.");
      haptic("error");
    }).catch(function () {
      busy(false);
      setText($("err"), "Нет связи. Ничего не потеряно: нажмите «Записать» ещё раз, дубля не будет.");
      haptic("error");
    });
  }

  // форма открыта из карточки обмена («Исправить» / «Записать»): подставляем значения черновика
  function applyExchangeDraft(b) {
    var x = freshExchange();
    x.giveAmount = b.give.amount; x.giveCur = b.give.currency; x.giveMethod = b.give.method || "";
    x.legs = b.get.map(function (g) {
      return { amount: g.amount, cur: g.currency, method: g.method || "", part: g.part || "" };
    });
    if (b.fee) { x.feeAmount = b.fee.amount; x.feeCur = b.fee.currency; }
    x.note = b.note || "";
    x.dateKey = b.date_key; x.datePick = b.date_key === "pick" ? b.date : "";
    state.x = x; state.dir = "ex"; state.draftId = b.id;
    showTab("record");
    touched(); render();
  }

  // правка из вкладки «Обмены»: форма заполняется записанным обменом, «Сохранить» уходит PATCH-ем
  function editExchange(it) {
    var x = freshExchange();
    x.editId = it.id;
    x.giveAmount = centsToInput(it.give.cents); x.giveCur = it.give.currency; x.giveMethod = it.give.method || "";
    var multi = it.legs.length > 1;
    x.legs = it.legs.map(function (l) {
      return { amount: centsToInput(l.get_cents), cur: l.get_currency, method: l.get_method || "",
               part: multi && !l.estimated && l.give_part_cents ? centsToInput(l.give_part_cents) : "" };
    });
    if (it.fee) { x.feeAmount = centsToInput(it.fee.cents); x.feeCur = it.fee.currency; }
    x.note = it.note || "";
    var today = state.meta.today;
    x.dateKey = it.occurred_on === today ? "today" : it.occurred_on === shiftDate(today, -1) ? "yesterday" : "pick";
    x.datePick = x.dateKey === "pick" ? it.occurred_on : "";
    state.x = x; state.dir = "ex"; state.draftId = null;
    showTab("record");
    touched(); render();
  }

  function rateText(giveCur, leg) {
    if (leg.rate === null || leg.rate === undefined) { return "1 " + leg.get_currency + ": курс не посчитан"; }
    return (leg.estimated ? "≈ " : "") + "1 " + leg.get_currency + " = " + fmtRate(leg.rate) + " " + giveCur +
      (leg.estimated ? " (доли не указаны)" : "");
  }

  function exchangeRow(it) {
    var li = el("li", "rec" + (it.status === "cancelled" ? " gone" : "") + (it.closed ? "" : " tappable"));
    var top = el("div", "rec-top");
    var got = it.legs.map(function (l) { return fmtCents(l.get_cents) + " " + l.get_currency; }).join(", ");
    top.appendChild(el("span", "rec-cat", fmtCents(it.give.cents) + " " + it.give.currency + " → " + got));
    top.appendChild(el("span", "amt", shortDate(it.occurred_on)));
    li.appendChild(top);
    it.legs.forEach(function (l) { li.appendChild(el("div", "rec-sub rate", rateText(it.give.currency, l))); });
    var sub = [];
    if (it.fee) { sub.push("комиссия " + fmtCents(it.fee.cents) + " " + it.fee.currency); }
    if (it.give.method_title) { sub.push("отдал: " + it.give.method_title); }
    it.legs.forEach(function (l) { if (l.get_method_title) { sub.push("получил " + l.get_currency + ": " + l.get_method_title); } });
    if (it.note) { sub.push(it.note); }
    if (it.status === "cancelled") { sub.push("удалён"); }
    if (it.closed) { sub.push("месяц закрыт"); }
    if (sub.length) { li.appendChild(el("div", "rec-sub", sub.join(" · "))); }
    if (it.closed) { return li; }
    li.setAttribute("tabindex", "0");
    var toggle = function () {
      var open = li.querySelector(".acts");
      if (open) { li.removeChild(open); li.removeAttribute("aria-expanded"); return; }
      li.setAttribute("aria-expanded", "true");
      var box = el("div", "acts");
      var btn = function (text, cls, fn) {
        var b = el("button", "mini " + cls, text); b.type = "button"; b.addEventListener("click", fn); box.appendChild(b);
      };
      if (it.status === "cancelled") {
        btn("Вернуть", "", function () { exAction(it.id, "restore", "Обмен №" + it.id + " возвращён."); });
      } else {
        btn("Изменить", "", function () { editExchange(it); });
        btn("Удалить", "danger", function () {
          clear(box);
          box.appendChild(el("span", "ask", "Удалить обмен №" + it.id + "?"));
          btn("Да, удалить", "danger", function () { exAction(it.id, "cancel", "Обмен №" + it.id + " удалён."); });
          btn("Нет", "", function () { li.removeChild(box); li.removeAttribute("aria-expanded"); toggle(); });
        });
      }
      li.appendChild(box);
    };
    li.addEventListener("click", function (ev) {
      var inside = ev.composedPath().some(function (n) { return n.classList && n.classList.contains("acts"); });
      if (!inside) { toggle(); }
    });
    return li;
  }

  function exAction(id, action, doneText) {
    postJson("api/exchanges/" + id + "/" + action, {}).then(function (res) {
      if (res.status === 200 && res.body.ok) { setMsg("exchanges-msg", doneText, false); loadExchanges(); return; }
      setMsg("exchanges-msg", ERRORS[(res.body || {}).error] || "Не получилось. Попробуйте ещё раз.", true);
    }).catch(function () { setMsg("exchanges-msg", "Нет связи. Ничего не изменено.", true); });
  }

  function loadExchanges() {
    var list = $("exchanges-list");
    if (!state.exMonth) { state.exMonth = state.meta.today.slice(0, 7); }
    return api("api/exchanges?month=" + state.exMonth).then(function (res) {
      if (res.status !== 200) {
        clear(list);
        list.appendChild(el("li", "rec empty", res.status === 401 ? "Откройте Кассу заново." :
          res.status === 403 ? "Нет доступа." : "Не удалось загрузить. Попробуйте ещё раз."));
        return;
      }
      $("ex-title").textContent = "Обмены: " + monthTitle(state.exMonth).toLowerCase();
      $("ex-next").disabled = state.exMonth >= state.meta.today.slice(0, 7);
      clear(list);
      if (!res.body.items.length) { list.appendChild(el("li", "rec empty", "В этом месяце обменов нет")); }
      res.body.items.forEach(function (it) { list.appendChild(exchangeRow(it)); });
    }).catch(function () {
      clear(list);
      list.appendChild(el("li", "rec empty", "Нет связи. Попробуйте ещё раз."));
    });
  }

  function setPeriodCount(box, n) {
    box.textContent = n > 0 ? "Обменов: " + n : "";
    show(box, n > 0);
  }

  // --- вкладки ----------------------------------------------------------------------------------------------
  function showTab(name) {
    state.tab = name;
    Array.prototype.forEach.call($("tabs").children, function (b) {
      b.className = b.getAttribute("data-tab") === name ? "on" : "";
    });
    show($("form"), name === "record");
    show($("view-inbox"), name === "inbox");
    show($("view-today"), name === "today");
    show($("view-month"), name === "month");
    show($("view-period"), name === "period");
    show($("view-recent"), name === "recent");
    show($("view-exchanges"), name === "exchanges");
    if (tg && tg.MainButton) { if (name === "record") { tg.MainButton.show(); } else { tg.MainButton.hide(); } }
    refreshButton();
    window.scrollTo(0, 0);
    if (name === "inbox") { setMsg("inbox-msg", "", false); loadInbox(); }
    if (name === "today") { loadToday(); }
    if (name === "month") { loadMonth(); }
    if (name === "period") { loadPeriod(); }
    if (name === "today") { setMsg("today-msg", "", false); }
    if (name === "recent") { setMsg("recent-msg", "", false); loadRecent(); }
    if (name === "exchanges") { setMsg("exchanges-msg", "", false); loadExchanges(); }
  }

  function loadCatalog() {
    return fetch("currencies.json", { cache: "no-store" }).then(function (r) { return r.json(); })
      .then(function (list) { state.catalog = list; })
      .catch(function () { state.catalog = []; });
  }

  function loadMeta(quiet) {
    return api("api/meta").then(function (res) {
      if (res.status === 401) { setText($("stub"), "Откройте Кассу заново."); return; }
      if (res.status === 403) { setText($("stub"), "Нет доступа."); return; }
      if (res.status !== 200) { setText($("stub"), "Касса недоступна. Ничего не записано."); return; }
      var first = !state.meta;
      state.meta = res.body;
      if (first || state.syncMethod) {
        var lm = res.body.last_method || {};
        if (first) { state.methodBy = { out: lm.out || "", in: lm.in || "" }; }
        else { state.methodBy[state.dir] = lm[state.dir] || ""; }
        state.method = state.methodBy[state.dir] || "";
        state.syncMethod = false;
      }
      if (first) {
        state.cur.out = res.body.last_currency.out;
        state.cur.in = res.body.last_currency.in;
        state.catAuto = res.body.defaults.out;
        state.clientId = uuid();
        state.x = freshExchange();
      }
      show($("badge-test"), !!res.body.test);
      show($("stub"), false);
      show($("tabs"), true);
      if (first) {
        if (tg && tg.MainButton) {
          tg.MainButton.setText("Записать");
          tg.MainButton.show();
          tg.MainButton.onClick(submit);
        }
        showTab("record");
      }
      render();
      setButton("Записать");
    }).catch(function () {
      if (!quiet) { setText($("stub"), "Касса недоступна. Ничего не записано."); }
    });
  }

  // --- голос и карточки чата ---
  function dictate() {
    var msg = $("dictate-msg");
    postJson("api/voice/arm", { direction: state.dir }).then(function (res) {
      if (res.status === 200 && res.body.ok) {
        setText(msg, "Готово: пришлите голосовое в чат, оно придёт карточкой.");
        show(msg, true);
        try { tg.close(); } catch (e) { /* не критично */ }
        return;
      }
      setText(msg, res.status === 401 ? "Откройте Кассу заново." : "Не получилось. Ничего не записано.");
      show(msg, true);
    }).catch(function () { setText(msg, "Нет связи. Ничего не записано."); show(msg, true); });
  }

  // форма открыта из карточки «Исправить»: подставляем значения черновика
  function applyDraft(id, flip) {
    return api("api/draft?id=" + encodeURIComponent(id)).then(function (res) {
      var b = res.body || {};
      if (res.status === 409) { setText($("err"), "Карточка уже закрыта или устарела: запись не будет двойной."); return; }
      if (res.status !== 200 || !b.ok) { setText($("err"), "Не удалось открыть карточку."); return; }
      if (b.kind === "exchange") { return applyExchangeDraft(b); }
      state.draftId = b.id;
      state.dir = b.direction;
      state.cur[b.direction] = b.currency;
      state.method = b.method || ""; state.methodOther = false; state.methodBy[b.direction] = state.method;
      $("amount").value = b.amount; $("note").value = b.note || "";
      state.dateKey = b.date_key; state.datePick = b.date_key === "pick" ? b.date : ""; $("date-pick").value = state.datePick;
      state.segment = "";
      state.catOther = false; state.catManual = null; state.catAuto = b.category;
      var known = (state.meta.categories[b.direction] || []).indexOf(b.category) >= 0;
      if (known) { state.catManual = b.category; } else { state.catOther = true; $("cat-other").value = b.category; }
      if (flip) {                                   // «Это поступление / расход»: та же запись, другое направление
        var nd = b.direction === "out" ? "in" : "out";
        state.dir = nd; state.cur[nd] = b.currency;
        var mm = (state.meta.methods || []).filter(function (x) { return x.code === state.method; })[0];
        if (mm && mm.scope !== nd && mm.scope !== "both") { state.method = ""; }
        state.methodBy[nd] = state.method;
        state.catManual = null; state.catOther = false; $("cat-other").value = "";
      }
      show($("draft-banner"), true);
      touched(); render();
      if (flip) { scheduleCategory(true); }
    }).catch(function () { setText($("err"), "Нет связи. Карточка не открылась."); });
  }

  // --- запуск ---
  if (!tg) { setText($("stub"), "Скрипт Telegram не загрузился."); return; }
  tg.ready();
  tg.expand();
  if (!initData) { setText($("stub"), "Откройте Кассу кнопкой в чате с ботом."); return; }

  Array.prototype.forEach.call($("direction").children, function (b) {
    b.addEventListener("click", function () {
      var d = b.getAttribute("data-dir");
      if (d === state.dir || !state.meta) { return; }
      if (d === "ex" || state.dir === "ex") { state.draftId = null; show($("draft-banner"), false); }   // черновик не переезжает между видами
      if (d === "ex") { state.dir = "ex"; touched(); render(); return; }
      state.dir = d;
      state.catManual = null; state.catOther = false; state.catAuto = null; $("cat-other").value = "";
      state.method = state.methodBy[d] || ""; state.methodOther = false; $("method-other").value = "";
      state.showMoreMethod = false;
      touched(); render();
      scheduleCategory(true);
    });
  });
  Array.prototype.forEach.call($("tabs").children, function (b) {
    b.addEventListener("click", function () { if (state.meta) { showTab(b.getAttribute("data-tab")); } });
  });
  ["amount", "note", "cat-other", "method-other"].forEach(function (id) {
    $(id).addEventListener("input", touched);
  });
  $("note").addEventListener("input", function () { scheduleCategory(false); });
  $("cat-select").addEventListener("change", function () {
    var v = $("cat-select").value;
    if (v === "__other") { state.catOther = true; state.catManual = null; }
    else { state.catOther = false; state.catManual = v; }
    touched(); render();
    if (v === "__other") { $("cat-other").focus(); }
  });
  $("method-select").addEventListener("change", function () {
    var v = $("method-select").value;
    if (v === "__other") { state.methodOther = true; state.method = ""; state.showMoreMethod = true; touched(); render(); $("method-other").focus(); return; }
    if (v) { pickMethod(v); return; }
    state.methodOther = false; state.method = ""; state.methodBy[state.dir] = "";
    touched(); render();
  });
  $("x-give-amount").addEventListener("input", function () { state.x.giveAmount = $("x-give-amount").value; exTouched(); });
  $("x-give-cur").addEventListener("change", function () {
    state.x.giveCur = $("x-give-cur").value;
    if (!state.x.feeCurTouched) { state.x.feeCur = ""; fillCurSelect($("x-fee-cur"), state.x.giveCur); }
    exTouched();
  });
  $("x-give-method").addEventListener("change", function () { state.x.giveMethod = $("x-give-method").value; exTouched(); });
  $("x-fee-amount").addEventListener("input", function () { state.x.feeAmount = $("x-fee-amount").value; exTouched(); });
  $("x-fee-cur").addEventListener("change", function () {
    state.x.feeCur = $("x-fee-cur").value; state.x.feeCurTouched = true; exTouched();
  });
  $("x-note").addEventListener("input", function () { state.x.note = $("x-note").value; exTouched(); });
  $("x-date-pick").addEventListener("change", function () {
    state.x.datePick = $("x-date-pick").value; renderExchangeForm(); exTouched();
  });
  $("x-add-leg").addEventListener("click", function () {
    var used = state.x.legs.map(function (l) { return l.cur; }).concat([state.x.giveCur]);
    var next = curCodes().filter(function (c) { return used.indexOf(c) < 0; })[0] || "USD";
    state.x.legs.push({ amount: "", cur: next, method: "", part: "" });
    renderLegs(); exTouched();
  });
  $("ex-prev").addEventListener("click", function () { state.exMonth = shiftMonth(state.exMonth, -1); loadExchanges(); });
  $("ex-next").addEventListener("click", function () {
    var next = shiftMonth(state.exMonth, 1);
    if (next <= state.meta.today.slice(0, 7)) { state.exMonth = next; loadExchanges(); }
  });
  $("cur-search").addEventListener("input", renderResults);
  $("undo").addEventListener("click", undoLast);
  $("dictate").addEventListener("click", dictate);
  $("date-pick").addEventListener("change", function () { state.datePick = $("date-pick").value; touched(); render(); });
  $("month-rates-toggle").addEventListener("click", function () {
    state.showRates = !state.showRates;
    $("month-rates-toggle").textContent = state.showRates ? "Курсы ▴" : "Курсы ▾";
    $("month-rates-toggle").setAttribute("aria-expanded", String(state.showRates));
    show($("month-rates"), state.showRates);
  });
  $("month-cur-toggle").addEventListener("click", function () {
    var open = $("month-totals").hidden;                      // скрыт -> раскрыть
    show($("month-totals"), open);
    $("month-cur-toggle").textContent = open ? "По валютам ▴" : "По валютам ▾";
    $("month-cur-toggle").setAttribute("aria-expanded", String(open));
  });
  $("period-from").addEventListener("change", loadPeriod);
  $("period-to").addEventListener("change", loadPeriod);
  $("period-7").addEventListener("click", function () { setPeriod(addDays(state.meta.today, -6), state.meta.today); });
  $("period-30").addEventListener("click", function () { setPeriod(addDays(state.meta.today, -29), state.meta.today); });
  $("period-cur-toggle").addEventListener("click", function () {
    var open = $("period-totals").hidden;
    show($("period-totals"), open);
    $("period-cur-toggle").textContent = open ? "По валютам ▴" : "По валютам ▾";
    $("period-cur-toggle").setAttribute("aria-expanded", String(open));
  });
  $("month-prev").addEventListener("click", function () { state.month = shiftMonth(state.month, -1); loadMonth(); });
  $("month-next").addEventListener("click", function () {
    var next = shiftMonth(state.month, 1);
    if (next <= state.meta.today.slice(0, 7)) { state.month = next; loadMonth(); }
  });
  loadCatalog().then(function () { return loadMeta(false); }).then(function () {
    if (!state.meta) { return; }
    var qs = window.location.search || "";
    var m = /[?&]draft=([0-9]{1,12})(?:&|$)/.exec(qs);
    loadInbox(true);
    if (m) { return applyDraft(m[1], /[?&]flip=1(?:&|$)/.test(qs)); }
    var tab = /[?&]tab=(recent|inbox|today|month|period|exchanges)(?:&|$)/.exec(qs);
    if (tab) { showTab(tab[1]); }
  });
})();
