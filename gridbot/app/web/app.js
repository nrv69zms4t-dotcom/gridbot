/* GridBot frontend — ванильный JS, без внешних ресурсов.
   Общение с ядром: window.pywebview.api (pywebview js_api). */
'use strict';

var api = null;
var lastStatus = null;
var shownStopError = null;
var pollTimer = null;
var statusInFlight = false;
var btTimer = null;

/* --- candlestick chart (TradingView Lightweight Charts™) state --- */
var chart = null;             // main chart instance
var candleSeries = null;
var chartSymbol = null;       // symbol currently shown on the chart
var chartInterval = '1m';
var chartTimer = null;
var chartInFlight = false;    // pause the poll while a fetch is running
var chartHasData = false;
var chartNeedsFit = false;
var chartCandlesSig = '';
var chartFirstTime = 0;       // unix sec of the first loaded candle
var gridLines = [];           // active createPriceLine handles
var gridLinesSig = '';
var markersSig = '';
var symbolDebounce = null;
var btChart = null;           // backtest modal chart
var btGridSeries = null;
var btBhSeries = null;

var CHART_POLL_MS = 10000;
var INTERVAL_SEC = { '1m': 60, '5m': 300, '15m': 900, '1h': 3600 };

function $(sel) { return document.querySelector(sel); }

/* ------------------------------------------------------------------ core */

function showNoCore(show) {
  var el = $('#nocore');
  if (el) el.hidden = !show;
}

window.addEventListener('pywebviewready', function () {
  if (api) return;
  api = window.pywebview.api;
  showNoCore(false);
  init();
});

/* деградация без бэкенда: если ядро не появилось за 3 с — сообщение */
setTimeout(function () {
  if (api) return;
  if (window.pywebview && window.pywebview.api) {
    api = window.pywebview.api;
    showNoCore(false);
    init();
  } else {
    showNoCore(true);
  }
}, 3000);

function guard(fn) {
  if (!api) {
    showError('Нет соединения с ядром приложения.');
    return;
  }
  try {
    var p = fn();
    if (p && p.catch) {
      p.catch(function (e) {
        showError('Ошибка вызова ядра: ' + (e && e.message ? e.message : e));
      });
    }
  } catch (e) {
    showError('Ошибка вызова ядра: ' + e);
  }
}

/* ------------------------------------------------------------------ init */

function init() {
  api.get_defaults().then(function (d) {
    if (!d || !d.ok) return;
    applyConfig(d.config || {});
    if (d.has_saved_state) {
      $('#restore-symbol').textContent =
        d.saved_state_symbol ? ' (' + d.saved_state_symbol + ')' : '';
      $('#restore-box').hidden = false;
    }
    if (d.config && d.config.symbol) setChartSymbol(d.config.symbol);
  }).catch(function () {});
  refreshKeysStatus();
  loadSymbolList();
  startPolling();
  startChartPolling();
}

/* ------------------------------------------------------------- mode/live */

function currentMode() {
  return $('#mode-live').checked ? 'live' : 'paper';
}

function currentNetwork() {
  return $('#f-network').value === 'mainnet' ? 'mainnet' : 'testnet';
}

function updateModeUi() {
  var live = currentMode() === 'live';
  $('#live-net-box').hidden = !live;
  $('#btn-start').innerHTML = live
    ? '&#9654; Старт (LIVE)'
    : '&#9654; Старт (paper)';
}

$('#mode-paper').addEventListener('change', updateModeUi);
$('#mode-live').addEventListener('change', updateModeUi);

/* ---------------------------------------------------------------- keys */

function refreshKeysStatus() {
  if (!api || !api.get_api_status) return;
  api.get_api_status().then(function (r) {
    if (!r || !r.ok) return;
    if (r.saved) {
      $('#keys-status').textContent = r.key_preview
        ? 'Сохранён ключ ' + r.key_preview + '. Секрет не отображается.'
        : 'Ключи сохранены, но не читаются — сохраните заново.';
    } else {
      $('#keys-status').textContent = 'Ключи не сохранены.';
    }
  }).catch(function () {});
}

$('#btn-keys-save').addEventListener('click', function () {
  clearError();
  var key = $('#f-api-key').value.trim();
  var secret = $('#f-api-secret').value.trim();
  guard(function () {
    return api.save_api_keys(key, secret).then(function (r) {
      if (!r.ok) { $('#keys-info').textContent = r.error; return; }
      $('#f-api-key').value = '';
      $('#f-api-secret').value = '';
      $('#keys-info').textContent = 'Ключи сохранены (зашифрованы DPAPI).';
      refreshKeysStatus();
    });
  });
});

$('#btn-keys-test').addEventListener('click', function () {
  clearError();
  var btn = $('#btn-keys-test');
  btn.disabled = true;
  $('#keys-info').textContent = 'Проверка…';
  guard(function () {
    return api.test_connection(currentNetwork()).then(function (r) {
      btn.disabled = false;
      if (!r.ok) { $('#keys-info').textContent = r.error; return; }
      var parts = [];
      Object.keys(r.balances || {}).forEach(function (a) {
        parts.push(a + ': ' + fmt(r.balances[a], 8));
      });
      $('#keys-info').textContent = 'Подключение к ' + r.network +
        ' OK (' + fmt(r.ping_ms, 0) + ' мс). Балансы — ' +
        (parts.length ? parts.join(' · ') : 'нет средств');
    });
  });
  setTimeout(function () { btn.disabled = false; }, 20000);
});

$('#btn-keys-clear').addEventListener('click', function () {
  clearError();
  guard(function () {
    return api.clear_api_keys().then(function (r) {
      $('#keys-info').textContent = r.ok
        ? 'Ключи удалены.' : r.error;
      refreshKeysStatus();
    });
  });
});

function applyConfig(c) {
  if (c.symbol != null) $('#f-symbol').value = c.symbol;
  if (c.lower != null) $('#f-lower').value = c.lower;
  if (c.upper != null) $('#f-upper').value = c.upper;
  if (c.levels != null) $('#f-levels').value = c.levels;
  if (c.spacing != null) $('#f-spacing').value = c.spacing;
  if (c.budget != null) $('#f-budget').value = c.budget;
  if (c.fee != null) $('#f-fee').value = String(Math.round(c.fee * 100 * 1e6) / 1e6);
  if (c.poll != null) $('#f-poll').value = c.poll;
  /* режим сетки (старые config.json без этих полей = ручной режим) */
  if (c.step_pct != null) $('#f-step').value = c.step_pct;
  $('#gm-step').checked = c.grid_mode === 'step';
  $('#gm-manual').checked = c.grid_mode !== 'step';
  updateGridModeUi();
  updateChips();
}

/* ------------------------------------------------------- grid mode (step) */

function currentGridMode() {
  return $('#gm-step').checked ? 'step' : 'manual';
}

function updateGridModeUi() {
  var step = currentGridMode() === 'step';
  $('#bounds-row').hidden = step;
  $('#step-row').hidden = !step;
  if (!step) $('#step-hint').textContent = '';
}

$('#gm-manual').addEventListener('change', updateGridModeUi);
$('#gm-step').addEventListener('change', updateGridModeUi);

/* В режиме «От цены + шаг» границы считает ядро (compute_range) от
   текущей цены; заполняем скрытые lower/upper и продолжаем обычный
   поток (валидация не меняется). В ручном режиме — сразу дальше. */
function prepareRange(then) {
  if (currentGridMode() !== 'step') { then(); return; }
  var cfg = readCfg();
  guard(function () {
    return api.compute_range(cfg.symbol, cfg.step_pct, cfg.levels,
                             cfg.spacing).then(function (r) {
      if (!r.ok) { showError(r.error); return; }
      $('#f-lower').value = r.lower;
      $('#f-upper').value = r.upper;
      $('#step-hint').textContent =
        'Сетка: ' + r.levels + ' уровней, от ' + fmt(r.lower, 2) +
        ' до ' + fmt(r.upper, 2) + ' (шаг ≈ ' + fmt(r.step_abs, 2) + ')';
      then();
    });
  });
}

/* ------------------------------------------------------- symbol picker */

function updateChips() {
  var current = $('#f-symbol').value.trim().toUpperCase();
  document.querySelectorAll('#symbol-chips .chip').forEach(function (chip) {
    chip.classList.toggle('active',
      chip.getAttribute('data-symbol') === current);
  });
}

document.querySelectorAll('#symbol-chips .chip').forEach(function (chip) {
  chip.addEventListener('click', function () {
    if (lastStatus && lastStatus.running) return;
    var s = chip.getAttribute('data-symbol');
    $('#f-symbol').value = s;
    updateChips();
    setChartSymbol(s);
  });
});

/* автодополнение пары: список USDT-пар из ядра (сеть → кэш → встроенный),
   грузится один раз при старте, не блокирует интерфейс */
function loadSymbolList() {
  if (!api || !api.list_symbols) return;
  api.list_symbols().then(function (r) {
    if (!r || !r.ok || !r.symbols) return;
    var dl = $('#symbols-list');
    dl.innerHTML = '';
    r.symbols.forEach(function (s) {
      var opt = document.createElement('option');
      opt.value = s;
      dl.appendChild(opt);
    });
  }).catch(function () {});
}

/* ------------------------------------------------------------------ form */

function numVal(sel) {
  var raw = String($(sel).value).trim().replace(',', '.');
  if (raw === '') return null;
  var v = Number(raw);
  return isFinite(v) ? v : null;
}

function readCfg() {
  var feePct = numVal('#f-fee');
  var levels = numVal('#f-levels');
  return {
    symbol: $('#f-symbol').value.trim().toUpperCase(),
    lower: numVal('#f-lower'),
    upper: numVal('#f-upper'),
    levels: levels == null ? null : Math.round(levels),
    spacing: $('#f-spacing').value,
    budget: numVal('#f-budget'),
    fee: feePct == null ? null : feePct / 100,
    poll: numVal('#f-poll'),
    grid_mode: currentGridMode(),
    step_pct: numVal('#f-step')
  };
}

function showError(msg) {
  var el = $('#form-error');
  el.textContent = msg;
  el.hidden = false;
}

function clearError() {
  var el = $('#form-error');
  el.textContent = '';
  el.hidden = true;
}

/* --------------------------------------------------------------- buttons */

$('#btn-price').addEventListener('click', function () {
  clearError();
  var symbol = $('#f-symbol').value.trim().toUpperCase();
  var btn = $('#btn-price');
  btn.disabled = true;
  guard(function () {
    return api.fetch_market(symbol).then(function (r) {
      btn.disabled = false;
      if (!r.ok) { showError(r.error); return; }
      $('#market-info').textContent =
        'Цена ' + r.symbol + ': ' + fmt(r.price, 2) +
        ' · тик ' + r.filters.price_step +
        ' · шаг лота ' + r.filters.qty_step +
        ' · мин. ордер ' + r.filters.min_notional + ' USDT' +
        (r.filters.from_network ? '' : ' (офлайн-фильтры)');
      $('#hdr-symbol').textContent = r.symbol;
      $('#hdr-price').textContent = fmt(r.price, 2);
      setChartSymbol(r.symbol);
    });
  });
  setTimeout(function () { btn.disabled = false; }, 8000);
});

/* смена символа в форме (не в работе) — график следует за полем, ~1 с */
$('#f-symbol').addEventListener('input', function () {
  updateChips();
  if (symbolDebounce) clearTimeout(symbolDebounce);
  symbolDebounce = setTimeout(function () {
    symbolDebounce = null;
    if (lastStatus && lastStatus.running) return;
    var s = $('#f-symbol').value.trim().toUpperCase();
    if (s) setChartSymbol(s);
  }, 1000);
});

$('#btn-range').addEventListener('click', function () {
  clearError();
  var symbol = $('#f-symbol').value.trim().toUpperCase();
  var btn = $('#btn-range');
  btn.disabled = true;
  btn.textContent = 'Подбор…';
  var restore = function () {
    btn.disabled = false;
    btn.textContent = 'Подобрать диапазон';
  };
  guard(function () {
    return api.suggest_range(symbol).then(function (r) {
      restore();
      if (!r.ok) { showError(r.error); return; }
      $('#f-lower').value = r.lower;
      $('#f-upper').value = r.upper;
      $('#market-info').textContent =
        'Диапазон за ' + r.days + ' дней: ' + fmt(r.lower, 2) + ' — ' +
        fmt(r.upper, 2) + ' · текущая цена ' + fmt(r.current, 2);
    });
  });
  setTimeout(restore, 30000);
});

$('#btn-start').addEventListener('click', function () {
  clearError();
  /* в режиме «От цены + шаг» сперва считаем границы от текущей цены */
  prepareRange(function () {
    if (currentMode() === 'live') {
      var net = currentNetwork();
      if (net === 'mainnet') {
        /* red confirmation modal: user must type the symbol name */
        var symbol = readCfg().symbol || '';
        $('#live-confirm-symbol').textContent = symbol || '—';
        $('#f-live-confirm').value = '';
        $('#live-modal-error').hidden = true;
        $('#live-overlay').hidden = false;
        $('#f-live-confirm').focus();
        return;
      }
      startLive('testnet', '');
      return;
    }
    guard(function () {
      return api.start_paper(readCfg()).then(function (r) {
        if (!r.ok) { showError(r.error); return; }
        $('#restore-box').hidden = true;
        pollNow();
      });
    });
  });
});

function startLive(network, confirmText) {
  guard(function () {
    return api.start_live(readCfg(), network, confirmText).then(function (r) {
      if (!r.ok) {
        if (!$('#live-overlay').hidden) {
          var el = $('#live-modal-error');
          el.textContent = r.error;
          el.hidden = false;
        } else {
          showError(r.error);
        }
        return;
      }
      $('#live-overlay').hidden = true;
      $('#restore-box').hidden = true;
      pollNow();
    });
  });
}

$('#btn-live-go').addEventListener('click', function () {
  startLive('mainnet', $('#f-live-confirm').value.trim());
});
$('#btn-live-cancel').addEventListener('click', function () {
  $('#live-overlay').hidden = true;
});

$('#btn-stop').addEventListener('click', function () {
  clearError();
  if (lastStatus && lastStatus.mode === 'live') {
    $('#f-stop-cancel').checked = true;
    $('#stop-overlay').hidden = false;
    return;
  }
  guard(function () {
    return api.stop_paper().then(function (r) {
      if (!r.ok) { showError(r.error); return; }
      pollNow();
    });
  });
});

$('#btn-stop-confirm').addEventListener('click', function () {
  var cancelOrders = $('#f-stop-cancel').checked;
  var btn = $('#btn-stop-confirm');
  btn.disabled = true;
  btn.textContent = 'Останавливаем…';
  var restore = function () {
    btn.disabled = false;
    btn.textContent = 'Остановить';
  };
  guard(function () {
    return api.stop_live(cancelOrders).then(function (r) {
      restore();
      $('#stop-overlay').hidden = true;
      if (!r.ok) { showError(r.error); return; }
      pollNow();
    });
  });
  setTimeout(restore, 70000);
});
$('#btn-stop-abort').addEventListener('click', function () {
  $('#stop-overlay').hidden = true;
});

$('#btn-resume').addEventListener('click', function () {
  clearError();
  guard(function () {
    return api.resume_paper().then(function (r) {
      if (!r.ok) { showError(r.error); return; }
      $('#restore-box').hidden = true;
      pollNow();
    });
  });
});

$('#btn-discard').addEventListener('click', function () {
  clearError();
  guard(function () {
    return api.discard_saved_state().then(function () {
      $('#restore-box').hidden = true;
    });
  });
});

/* -------------------------------------------------------------- backtest */

function setBtRunning(running) {
  var btn = $('#btn-backtest');
  btn.disabled = running;
  btn.textContent = running ? 'Бэктест выполняется…' : 'Бэктест 60 дней';
}

$('#btn-backtest').addEventListener('click', function () {
  clearError();
  prepareRange(function () {
    guard(function () {
      return api.run_backtest(readCfg()).then(function (r) {
        if (!r.ok) { showError(r.error); return; }
        setBtRunning(true);
        if (btTimer) clearInterval(btTimer);
        btTimer = setInterval(checkBacktest, 800);
      });
    });
  });
});

function checkBacktest() {
  if (!api) return;
  api.get_backtest_status().then(function (r) {
    if (r && r.running) return;
    clearInterval(btTimer);
    btTimer = null;
    setBtRunning(false);
    if (!r || !r.ok) {
      showError((r && r.error) || 'Ошибка бэктеста.');
      return;
    }
    if (r.report) showBacktestModal(r.report);
  }).catch(function () {
    clearInterval(btTimer);
    btTimer = null;
    setBtRunning(false);
  });
}

function showBacktestModal(rep) {
  $('#bt-symbol').textContent = rep.symbol ? '— ' + rep.symbol : '';
  var worse = rep.total_return < rep.buyhold_return;
  var warn = $('#bt-warning');
  warn.hidden = !worse;
  if (worse) {
    warn.textContent =
      'Внимание: за этот период простая покупка и удержание (' +
      fmt(rep.buyhold_return, 2) + '%) принесли бы больше, чем сетка (' +
      fmt(rep.total_return, 2) + '%).';
  }
  var rows = [
    ['Доходность сетки', fmt(rep.total_return, 2) + '%'],
    ['Buy & Hold', fmt(rep.buyhold_return, 2) + '%'],
    ['Макс. просадка', fmt(rep.max_dd, 2) + '%'],
    ['Реализованный PnL', fmt(rep.realized, 2) + ' USDT'],
    ['Нереализованный PnL', fmt(rep.unrealized, 2) + ' USDT'],
    ['Комиссии', fmt(rep.fees, 2) + ' USDT'],
    ['Сделок / кругов', String(rep.n_trades) + ' / ' + String(rep.n_round_trips)],
    ['Время в диапазоне', fmt(rep.time_in_range, 1) + '%'],
    ['Свечей (1ч)', String(rep.n_candles)]
  ];
  var grid = $('#bt-metrics');
  grid.innerHTML = '';
  rows.forEach(function (row) {
    var d = document.createElement('div');
    d.className = 'bt-item';
    var b = document.createElement('b');
    b.textContent = row[1];
    d.textContent = row[0];
    d.appendChild(b);
    grid.appendChild(d);
  });
  /* окно должно быть видимым ДО отрисовки: график меряет контейнер */
  $('#modal-overlay').hidden = false;
  drawBtChart(rep);
}

$('#btn-modal-close').addEventListener('click', function () {
  $('#modal-overlay').hidden = true;
});
$('#modal-overlay').addEventListener('click', function (e) {
  if (e.target === $('#modal-overlay')) $('#modal-overlay').hidden = true;
});

/* --------------------------------------------------------------- polling */

function startPolling() {
  if (pollTimer) return;
  pollTimer = setInterval(pollNow, 1500);
  pollNow();
}

function pollNow() {
  if (!api || statusInFlight) return;
  statusInFlight = true;
  api.get_status().then(function (st) {
    statusInFlight = false;
    if (st && st.ok) render(st);
  }).catch(function () {
    statusInFlight = false;
  });
}

/* ---------------------------------------------------------------- render */

function fmt(x, d) {
  if (x == null || !isFinite(x)) return '—';
  if (d == null) d = 2;
  return Number(x).toLocaleString('ru-RU',
    { minimumFractionDigits: d, maximumFractionDigits: d });
}

function setPnl(sel, v, suffix) {
  var el = $(sel);
  if (v == null) { el.textContent = '—'; el.className = 'card-value'; return; }
  el.textContent = (v > 0 ? '+' : '') + fmt(v, 2) + (suffix || '');
  el.className = 'card-value ' + (v > 1e-9 ? 'pos' : (v < -1e-9 ? 'neg' : ''));
}

function render(st) {
  lastStatus = st;

  if (st.symbol) $('#hdr-symbol').textContent = st.symbol;
  $('#hdr-price').textContent = st.price != null ? fmt(st.price, 2) : '—';

  /* stale = paper работает, но свежих данных с биржи давно не было */
  var pollS = st.poll_seconds || 10;
  var stale = !!st.running && (st.net_error != null ||
    (st.data_age != null && st.data_age > Math.max(3 * pollS, 15)));
  $('#hdr-price').classList.toggle('stale', stale);

  var b = $('#status-badge');
  if (st.running) {
    if (st.mode === 'live') {
      var suffix = stale ? ' (нет связи)' :
        (st.in_range === false ? ' (вне диапазона)' : '');
      if (st.network === 'mainnet') {
        b.textContent = 'LIVE' + suffix;
        b.className = 'badge badge-live';
      } else {
        b.textContent = 'LIVE·testnet' + suffix;
        b.className = 'badge badge-warn';
      }
    } else if (stale) {
      b.textContent = 'Работает (нет связи с биржей)';
      b.className = 'badge badge-warn';
    } else if (st.in_range === false) {
      b.textContent = 'Вне диапазона';
      b.className = 'badge badge-warn';
    } else {
      b.textContent = 'Работает (paper)';
      b.className = 'badge badge-on';
    }
  } else {
    b.textContent = 'Остановлен';
    b.className = 'badge badge-stopped';
  }

  /* header banner reflects the running mode */
  var banner = $('#mode-banner');
  if (st.running && st.mode === 'live') {
    if (st.network === 'mainnet') {
      banner.textContent = 'LIVE: бот выставляет РЕАЛЬНЫЕ ордера на Binance';
      banner.classList.add('live-banner');
    } else {
      banner.textContent =
        'LIVE·testnet: реальные ордера в тестовой сети Binance';
      banner.classList.remove('live-banner');
    }
  } else {
    banner.textContent =
      'Режим paper: реальные ордера на биржу не выставляются';
    banner.classList.remove('live-banner');
  }

  /* paper-поток умер сам (крах шага движка) — показать причину */
  if (st.last_error && !st.running && st.last_error !== shownStopError) {
    shownStopError = st.last_error;
    showError(st.last_error);
  }

  $('#cfg-form').disabled = !!st.running;
  $('#mode-form').disabled = !!st.running;
  $('#btn-start').disabled = !!st.running;
  $('#btn-stop').disabled = !st.running;

  $('#m-equity').textContent =
    st.equity != null ? fmt(st.equity, 2) + ' USDT' : '—';
  setPnl('#m-realized', st.realized_pnl, ' USDT');
  setPnl('#m-unrealized', st.unrealized_pnl, ' USDT');
  $('#m-fees').textContent = st.fees != null ? fmt(st.fees, 2) + ' USDT' : '—';
  $('#m-trips').textContent =
    st.n_round_trips != null ? String(st.n_round_trips) : '—';
  $('#m-balance').textContent = st.cash != null
    ? fmt(st.cash, 2) + ' / ' + fmt(st.base, 6)
    : '—';

  /* exchange balances card (live only) */
  var exch = $('#card-exch');
  if (st.balances) {
    exch.hidden = false;
    $('#m-exch').textContent =
      fmt(st.balances.quote, 2) + ' ' + st.balances.quote_asset + ' / ' +
      fmt(st.balances.base, 6) + ' ' + st.balances.base_asset;
  } else {
    exch.hidden = true;
  }

  renderFills(st.recent_fills || []);

  var pre = $('#log');
  var text = (st.log_tail || []).join('\n');
  if (pre.textContent !== text) {
    pre.textContent = text;
    pre.scrollTop = pre.scrollHeight;
  }

  chartOnStatus(st);
}

function renderFills(fills) {
  var tbody = $('#fills tbody');
  var empty = $('#fills-empty');
  tbody.innerHTML = '';
  empty.hidden = fills.length > 0;
  for (var i = fills.length - 1; i >= 0; i--) {
    var f = fills[i];
    var tr = document.createElement('tr');
    var tds = [
      timeShort(f.time),
      f.side === 'BUY' ? 'Покупка' : 'Продажа',
      fmt(f.price, 2),
      fmt(f.qty, 6)
    ];
    for (var j = 0; j < tds.length; j++) {
      var td = document.createElement('td');
      td.textContent = tds[j];
      if (j === 1) td.className = f.side === 'BUY' ? 'side-buy' : 'side-sell';
      tr.appendChild(td);
    }
    tbody.appendChild(tr);
  }
}

function timeShort(t) {
  var ms = Date.parse(t);
  if (!isFinite(ms)) return String(t);
  return new Date(ms).toLocaleTimeString('ru-RU');
}

/* ------------------------------------------------- candlestick main chart
   TradingView Lightweight Charts™ — © TradingView, Inc.
   Свечи приходят из ядра (get_chart_data), уровни сетки — createPriceLine
   по открытым ордерам, исполнения — setMarkers. */

var CHART_THEME = {
  layout: {
    background: { type: 'solid', color: 'transparent' },
    textColor: '#7d8b99',
    fontFamily: '"Segoe UI", system-ui, sans-serif',
    fontSize: 11
  },
  grid: {
    vertLines: { color: 'rgba(42, 53, 66, 0.45)' },
    horzLines: { color: 'rgba(42, 53, 66, 0.45)' }
  },
  rightPriceScale: { borderColor: '#2a3542' },
  timeScale: {
    borderColor: '#2a3542',
    timeVisible: true,
    secondsVisible: false
  },
  crosshair: {
    vertLine: { color: '#4da3ff', labelBackgroundColor: '#1d242e' },
    horzLine: { color: '#4da3ff', labelBackgroundColor: '#1d242e' }
  }
};

function initChart() {
  if (chart || !window.LightweightCharts) return;
  var host = $('#chart');
  chart = LightweightCharts.createChart(host, CHART_THEME);
  candleSeries = chart.addCandlestickSeries({
    upColor: '#2ecc71',
    downColor: '#e05252',
    borderUpColor: '#2ecc71',
    borderDownColor: '#e05252',
    wickUpColor: '#2ecc71',
    wickDownColor: '#e05252'
  });
  sizeCharts();
}

function sizeCharts() {
  var host = $('#chart');
  if (chart && host.clientWidth > 0 && host.clientHeight > 0) {
    chart.resize(host.clientWidth, host.clientHeight);
  }
  var btHost = $('#bt-chart');
  if (btChart && btHost.clientWidth > 0) {
    btChart.resize(btHost.clientWidth, btHost.clientHeight || 240);
  }
}

function setChartSymbol(symbol) {
  symbol = String(symbol || '').trim().toUpperCase();
  if (!symbol || symbol === chartSymbol) return;
  chartSymbol = symbol;
  chartNeedsFit = true;
  refreshChartData();
}

/* кнопки интервала над графиком */
document.querySelectorAll('.int-btn').forEach(function (btn) {
  btn.addEventListener('click', function () {
    var iv = btn.getAttribute('data-interval');
    if (!iv || iv === chartInterval) return;
    chartInterval = iv;
    document.querySelectorAll('.int-btn').forEach(function (b) {
      b.classList.toggle('active', b === btn);
    });
    chartNeedsFit = true;
    refreshChartData();
  });
});

function startChartPolling() {
  if (chartTimer) return;
  chartTimer = setInterval(refreshChartData, CHART_POLL_MS);
  refreshChartData();
}

function refreshChartData() {
  /* таймер «на паузе», пока запрос в полёте (chartInFlight) */
  if (!api || !api.get_chart_data || chartInFlight) return;
  if (!chartSymbol || !chart) return;
  var sym = chartSymbol, iv = chartInterval;
  chartInFlight = true;
  api.get_chart_data(sym, iv).then(function (r) {
    chartInFlight = false;
    if (!r || !r.ok) {
      /* сеть упала — молча оставляем старый график, только лог */
      console.log('график: ' + ((r && r.error) || 'нет ответа ядра'));
      return;
    }
    if (r.symbol !== chartSymbol || r.interval !== chartInterval) return;
    setChartCandles(r.candles || []);
  }).catch(function (e) {
    chartInFlight = false;
    console.log('график: ошибка вызова ядра: ' + e);
  });
}

function setChartCandles(candles) {
  if (!candleSeries) return;
  var data = [];
  for (var i = 0; i < candles.length; i++) {
    var c = candles[i];
    if (c == null || !isFinite(c.time)) continue;
    data.push({ time: c.time, open: c.open, high: c.high,
                low: c.low, close: c.close });
  }
  if (!data.length) return;
  var sig = chartSymbol + '|' + chartInterval + '|' + data.length + '|' +
            data[0].time + '|' + data[data.length - 1].time + '|' +
            data[data.length - 1].close;
  if (sig === chartCandlesSig && !chartNeedsFit) return;
  chartCandlesSig = sig;
  chartFirstTime = data[0].time;
  candleSeries.setData(data);
  chartHasData = true;
  $('#chart-empty').hidden = true;
  /* маркеры могли ждать данных — пересадим на новые свечи */
  markersSig = '';
  if (lastStatus) updateFillMarkers(lastStatus.recent_fills || []);
  if (chartNeedsFit) {
    chartNeedsFit = false;
    chart.timeScale().fitContent();
  }
}

/* вызывается из render(): символ работающего бота, уровни, маркеры */
function chartOnStatus(st) {
  if (!chart) return;
  if (st.running && st.symbol) setChartSymbol(st.symbol);
  updateGridLines(st.open_orders || []);
  updateFillMarkers(st.recent_fills || []);
}

/* уровни сетки: BUY — зелёный пунктир, SELL — красный; перерисовка
   только при изменении набора ордеров (диф по сигнатуре, без мерцания) */
function updateGridLines(orders) {
  if (!candleSeries) return;
  var sig = orders.map(function (o) {
    return o.side + '@' + o.price;
  }).sort().join(';');
  if (sig === gridLinesSig) return;
  gridLinesSig = sig;
  gridLines.forEach(function (line) {
    try { candleSeries.removePriceLine(line); } catch (e) {}
  });
  gridLines = [];
  orders.forEach(function (o) {
    gridLines.push(candleSeries.createPriceLine({
      price: o.price,
      color: o.side === 'BUY'
        ? 'rgba(46, 204, 113, 0.65)' : 'rgba(224, 82, 82, 0.65)',
      lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: false,
      title: (o.side === 'BUY' ? 'BUY ' : 'SELL ') + o.price
    }));
  });
}

/* маркеры исполнений: BUY — стрелка вверх под баром, SELL — вниз над
   баром; время — unix-секунды UTC, привязка к свече интервала */
function updateFillMarkers(fills) {
  if (!candleSeries || !chartHasData) return;
  var intSec = INTERVAL_SEC[chartInterval] || 60;
  var markers = [];
  for (var i = 0; i < fills.length; i++) {
    var f = fills[i];
    var ms = Date.parse(f.time);
    if (!isFinite(ms)) continue;
    var t = Math.floor(ms / 1000 / intSec) * intSec;
    if (t < chartFirstTime) continue;   // раньше загруженных свечей
    markers.push({
      time: t,
      position: f.side === 'BUY' ? 'belowBar' : 'aboveBar',
      color: f.side === 'BUY' ? '#2ecc71' : '#e05252',
      shape: f.side === 'BUY' ? 'arrowUp' : 'arrowDown',
      text: f.side === 'BUY' ? 'B' : 'S'
    });
  }
  markers.sort(function (a, b) { return a.time - b.time; });
  var sig = markers.map(function (m) {
    return m.time + m.text;
  }).join(';');
  if (sig === markersSig) return;
  markersSig = sig;
  candleSeries.setMarkers(markers);
}

/* -------------------------------------------------------- backtest chart */

function drawBtChart(rep) {
  var host = $('#bt-chart');
  if (!window.LightweightCharts) return;
  if (!btChart) {
    btChart = LightweightCharts.createChart(host, CHART_THEME);
    btBhSeries = btChart.addLineSeries({
      color: '#7d8b99', lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      priceLineVisible: false, lastValueVisible: false
    });
    btGridSeries = btChart.addLineSeries({
      color: '#4da3ff', lineWidth: 2,
      priceLineVisible: false, lastValueVisible: false
    });
  }
  btChart.resize(host.clientWidth || 640, host.clientHeight || 240);

  function toLine(arr) {
    var out = [], prev = -Infinity;
    (arr || []).forEach(function (q) {
      var ms = Date.parse(q.t);
      if (!isFinite(ms) || q.v == null) return;
      var t = Math.floor(ms / 1000);
      if (t <= prev) return;            // время строго возрастает
      prev = t;
      out.push({ time: t, value: q.v });
    });
    return out;
  }
  btGridSeries.setData(toLine(rep.equity_curve));
  btBhSeries.setData(toLine(rep.buyhold_curve));
  btChart.timeScale().fitContent();
}

window.addEventListener('resize', sizeCharts);
initChart();
updateChips();
