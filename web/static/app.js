// FW-NetPulse Pro v2.0 Frontend Controller
document.addEventListener("DOMContentLoaded", () => {
  let pingChart = null;
  let soundEnabled = true;
  let audioCtx = null;
  let lastFreezeTime = 0;
  let activeTimeframe = 60; // 60s, 300s (5m), 600s (10m), 900s (15m)
  let lastChartSignature = "";
  const windowCharts = new Map();
  const clientCards = new Map();
  const endpointFreezeTimes = new Map();
  let activeMtrWindowId = null;
  let activeHistoryWindowId = null;
  let latestMultiClient = { windows: [], endpoints: {} };
  let latestFallbackRoute = [];
  let latestFallbackTarget = {};
  let latestFallbackFreezes = [];
  let pendingTelemetry = null;
  let selectionPointerDown = false;
  let requestedSelectedPid = null;
  const collapsedWindowCharts = new Set();
  const SETTINGS_STORAGE_KEY = "fw-netpulse-interface-v1";

  const defaultInterfacePreferences = {
    mainChart: true,
    windowCharts: true,
    clientList: true,
    optimizer: true,
    mtr: true,
    history: true
  };

  function loadSavedSettings() {
    try {
      const saved = JSON.parse(localStorage.getItem(SETTINGS_STORAGE_KEY) || "{}");
      return {
        interface: { ...defaultInterfacePreferences, ...(saved.interface || {}) },
        features: saved.features || {}
      };
    } catch (_) {
      return { interface: { ...defaultInterfacePreferences }, features: {} };
    }
  }

  const savedSettings = loadSavedSettings();
  let interfacePreferences = savedSettings.interface;

  // Feature Toggles state
  let featureFlags = {
    tcpPing: true,
    smartMtr: true,
    geoIp: true,
    multiClient: true,
    ...savedSettings.features
  };

  function saveSettings() {
    try {
      localStorage.setItem(SETTINGS_STORAGE_KEY, JSON.stringify({
        interface: interfacePreferences,
        features: featureFlags
      }));
    } catch (_) {
      // Панель продолжит работать, даже если браузер запретил localStorage.
    }
  }

  function hasSelectedDashboardText() {
    const selection = window.getSelection?.();
    if (!selection || selection.isCollapsed || !selection.toString()) return false;
    const app = document.querySelector(".app-container");
    if (!app || selection.rangeCount === 0) return false;
    const node = selection.getRangeAt(0).commonAncestorContainer;
    return app.contains(node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement);
  }

  function shouldPauseUiRefresh() {
    return selectionPointerDown || hasSelectedDashboardText();
  }

  function flushPendingTelemetry() {
    if (!pendingTelemetry || shouldPauseUiRefresh()) return;
    const data = pendingTelemetry;
    pendingTelemetry = null;
    document.body.classList.remove("copy-paused");
    updateTelemetry(data, true);
  }

  function showNotice(message, isError = false) {
    let notice = document.getElementById("dashboardNotice");
    if (!notice) {
      notice = document.createElement("div");
      notice.id = "dashboardNotice";
      notice.className = "dashboard-notice";
      document.body.appendChild(notice);
    }
    notice.textContent = message;
    notice.classList.toggle("error", isError);
    notice.classList.add("visible");
    clearTimeout(showNotice.timer);
    showNotice.timer = setTimeout(() => notice.classList.remove("visible"), 2600);
  }

  // 1. Web Audio Chime Synthesizer
  function playLagChime() {
    if (!soundEnabled) return;
    try {
      if (!audioCtx) {
        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      }
      if (audioCtx.state === 'suspended') {
        audioCtx.resume();
      }
      const osc = audioCtx.createOscillator();
      const gain = audioCtx.createGain();

      osc.type = "sine";
      osc.frequency.setValueAtTime(440, audioCtx.currentTime);
      osc.frequency.exponentialRampToValueAtTime(880, audioCtx.currentTime + 0.15);

      gain.gain.setValueAtTime(0.15, audioCtx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.01, audioCtx.currentTime + 0.25);

      osc.connect(gain);
      gain.connect(audioCtx.destination);

      osc.start();
      osc.stop(audioCtx.currentTime + 0.25);
    } catch (e) {
      console.warn("Audio chime error:", e);
    }
  }

  // 2. Initialize Chart.js
  function initChart() {
    const ctx = document.getElementById("pingChart").getContext("2d");
    const gradient = ctx.createLinearGradient(0, 0, 0, 240);
    gradient.addColorStop(0, "rgba(56, 189, 248, 0.35)");
    gradient.addColorStop(1, "rgba(56, 189, 248, 0.0)");

    pingChart = new Chart(ctx, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          {
            label: "ICMP пинг",
            data: [],
            borderColor: "#38bdf8",
            borderWidth: 2,
            backgroundColor: gradient,
            fill: true,
            tension: 0.28,
            pointRadius: 0,
            pointHoverRadius: 4,
            pointBackgroundColor: "#38bdf8"
          },
          {
            label: "TCP игровой порт",
            data: [],
            borderColor: "#c084fc",
            borderWidth: 1.8,
            fill: false,
            pointRadius: 0,
            tension: 0.28,
            // Пустая секундная корзина не является потерей и не должна рвать линию.
            spanGaps: true
          },
          {
            label: "Скользящее среднее",
            data: [],
            borderColor: "rgba(255, 255, 255, 0.3)",
            borderWidth: 1.2,
            borderDash: [4, 4],
            fill: false,
            pointRadius: 0,
            tension: 0.3
          },
          {
            label: "Скачок задержки",
            data: [],
            borderColor: "#f59e0b",
            backgroundColor: "#f59e0b",
            showLine: false,
            pointRadius: 3,
            pointHoverRadius: 6
          },
          {
            label: "Потеря ICMP",
            data: [],
            borderColor: "#ef4444",
            backgroundColor: "#ef4444",
            borderWidth: 3,
            fill: false,
            tension: 0.12,
            spanGaps: false,
            lossDataset: true,
            lossFlags: [],
            pointStyle: "rectRot",
            pointRadius: (context) => context.dataset.lossFlags?.[context.dataIndex] ? 5 : 0,
            pointHoverRadius: 7
          },
          {
            label: "Потеря TCP",
            data: [],
            borderColor: "#ef4444",
            backgroundColor: "#ef4444",
            borderWidth: 3,
            fill: false,
            tension: 0.12,
            spanGaps: false,
            lossDataset: true,
            lossFlags: [],
            pointStyle: "rectRot",
            pointRadius: (context) => context.dataset.lossFlags?.[context.dataIndex] ? 5 : 0,
            pointHoverRadius: 7
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 0 },
        plugins: {
          legend: { display: false },
          tooltip: {
            mode: "index",
            intersect: false,
            filter: (context) => {
              if (!context.dataset.lossDataset) return context.raw !== null;
              return Boolean(context.dataset.lossFlags?.[context.dataIndex]);
            },
            backgroundColor: "rgba(15, 23, 42, 0.9)",
            titleColor: "#94a3b8",
            bodyColor: "#fff",
            borderColor: "rgba(255,255,255,0.1)",
            borderWidth: 1,
            callbacks: {
              label: (context) => context.dataset.lossDataset
                ? ` ${context.dataset.label}: пакет не получил ответ`
                : ` ${context.dataset.label}: ${context.raw !== null ? context.raw + ' ms' : 'Нет измерения'}`
            }
          }
        },
        scales: {
          x: {
            grid: { color: "rgba(255, 255, 255, 0.04)" },
            ticks: { color: "#64748b", font: { size: 10 }, maxTicksLimit: activeTimeframe > 300 ? 8 : 12 }
          },
          y: {
            grid: { color: "rgba(255, 255, 255, 0.05)" },
            ticks: { color: "#94a3b8", font: { size: 11 }, callback: (val) => val + ' ms' },
            beginAtZero: true,
            suggestedMax: 80,
            grace: "10%"
          }
        }
      }
    });
  }

  // Строит красный отрезок через момент настоящей потери. Значение в точке
  // потери вычисляется только для высоты маркера и не считается измеренным RTT.
  function buildLossOverlay(values, lossFlags) {
    const overlay = new Array(values.length).fill(null);

    lossFlags.forEach((isLost, index) => {
      if (!isLost) return;

      let previousIndex = index - 1;
      while (previousIndex >= 0 && values[previousIndex] === null) previousIndex--;

      let nextIndex = index + 1;
      while (nextIndex < values.length && values[nextIndex] === null) nextIndex++;

      const previousValue = previousIndex >= 0 ? values[previousIndex] : null;
      const nextValue = nextIndex < values.length ? values[nextIndex] : null;
      const markerValue = previousValue !== null && nextValue !== null
        ? (previousValue + nextValue) / 2
        : (previousValue ?? nextValue ?? 10);

      overlay[index] = Math.round(markerValue * 10) / 10;
      if (previousIndex >= 0) overlay[previousIndex] = previousValue;
      if (nextIndex < values.length) overlay[nextIndex] = nextValue;
    });

    return overlay;
  }

  function createWindowChart(canvas) {
    const ctx = canvas.getContext("2d");
    const gradient = ctx.createLinearGradient(0, 0, 0, 185);
    gradient.addColorStop(0, "rgba(56, 189, 248, 0.24)");
    gradient.addColorStop(1, "rgba(56, 189, 248, 0.0)");
    return new Chart(ctx, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          { label: "ICMP", data: [], borderColor: "#38bdf8", borderWidth: 1.8, backgroundColor: gradient, fill: true, tension: 0.26, pointRadius: 0 },
          { label: "TCP", data: [], borderColor: "#c084fc", borderWidth: 1.6, fill: false, tension: 0.26, pointRadius: 0, spanGaps: true },
          { label: "Средний", data: [], borderColor: "rgba(255,255,255,.28)", borderWidth: 1, borderDash: [4, 4], fill: false, tension: 0.3, pointRadius: 0 },
          { label: "Скачок", data: [], borderColor: "#f59e0b", backgroundColor: "#f59e0b", showLine: false, pointRadius: 2.5 },
          { label: "Потеря ICMP", data: [], borderColor: "#ef4444", backgroundColor: "#ef4444", borderWidth: 2.6, fill: false, spanGaps: false, lossDataset: true, lossFlags: [], pointStyle: "rectRot", pointRadius: context => context.dataset.lossFlags?.[context.dataIndex] ? 4 : 0 },
          { label: "Потеря TCP", data: [], borderColor: "#ef4444", backgroundColor: "#ef4444", borderWidth: 2.6, fill: false, spanGaps: false, lossDataset: true, lossFlags: [], pointStyle: "rectRot", pointRadius: context => context.dataset.lossFlags?.[context.dataIndex] ? 4 : 0 }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 0 },
        plugins: {
          legend: { display: false },
          tooltip: {
            mode: "index",
            intersect: false,
            filter: context => context.dataset.lossDataset ? Boolean(context.dataset.lossFlags?.[context.dataIndex]) : context.raw !== null,
            callbacks: {
              label: context => context.dataset.lossDataset
                ? ` ${context.dataset.label}: пакет не получил ответ`
                : ` ${context.dataset.label}: ${context.raw !== null ? context.raw + ' мс' : 'нет измерения'}`
            }
          }
        },
        scales: {
          x: { grid: { color: "rgba(255,255,255,.035)" }, ticks: { color: "#64748b", font: { size: 9 }, maxTicksLimit: activeTimeframe > 300 ? 6 : 9 } },
          y: { beginAtZero: true, suggestedMax: 80, grace: "10%", grid: { color: "rgba(255,255,255,.045)" }, ticks: { color: "#94a3b8", font: { size: 10 }, callback: value => value + " мс" } }
        }
      }
    });
  }

  function updateWindowChart(chart, chartData, targetPort) {
    if (!chart || !Array.isArray(chartData?.labels)) return;
    const labels = chartData.labels;
    const icmp = chartData.icmp || [];
    const tcp = chartData.tcp || [];
    const icmpLoss = chartData.icmp_loss || [];
    const tcpLoss = chartData.tcp_loss || [];
    const spikes = chartData.spikes || [];
    const signature = JSON.stringify([
      labels.length,
      labels[labels.length - 1],
      icmp[icmp.length - 1],
      tcp[tcp.length - 1],
      icmpLoss[icmpLoss.length - 1],
      tcpLoss[tcpLoss.length - 1],
      activeTimeframe
    ]);
    if (chart.$netpulseSignature === signature) return;
    chart.$netpulseSignature = signature;
    chart.data.labels = labels;
    chart.data.datasets[0].data = icmp;
    chart.data.datasets[1].label = `TCP :${targetPort || '--'}`;
    chart.data.datasets[1].data = featureFlags.tcpPing ? tcp : [];
    chart.data.datasets[2].data = chartData.average || [];
    chart.data.datasets[3].data = spikes;
    chart.data.datasets[4].lossFlags = icmpLoss;
    chart.data.datasets[4].data = buildLossOverlay(icmp, icmpLoss);
    chart.data.datasets[5].lossFlags = tcpLoss;
    chart.data.datasets[5].data = featureFlags.tcpPing ? buildLossOverlay(tcp, tcpLoss) : [];
    chart.options.scales.x.ticks.maxTicksLimit = activeTimeframe > 300 ? 6 : 9;
    chart.update("none");
  }

  function markSelectedProcess(pid) {
    const selectedPid = Number(pid);
    for (const [clientPid, card] of clientCards.entries()) {
      const isSelected = clientPid === selectedPid;
      card.classList.toggle("selected", isSelected);
      card.setAttribute("aria-pressed", isSelected ? "true" : "false");
    }
    document.querySelectorAll("[data-window-chart-id]").forEach(card => {
      card.classList.toggle("selected", card.dataset.windowChartId === `pid-${selectedPid}`);
    });
    if ((latestMultiClient.windows || []).some(item => Number(item.pid) === selectedPid)) {
      latestMultiClient.active_window_id = `pid-${selectedPid}`;
    }
  }

  function renderClientCards(instances, selectedPid) {
    const grid = document.getElementById("clientsGrid");
    const activePids = new Set(instances.map(item => Number(item.pid)));

    for (const [pid, card] of clientCards.entries()) {
      if (!activePids.has(pid)) {
        card.remove();
        clientCards.delete(pid);
      }
    }

    instances.forEach((instance, index) => {
      const pid = Number(instance.pid);
      let card = clientCards.get(pid);
      if (!card) {
        card = document.createElement("div");
        card.className = "client-card-item";
        card.dataset.clientPid = String(pid);
        card.tabIndex = 0;
        card.setAttribute("role", "button");
        card.innerHTML = `
          <div class="client-card-title">
            <span class="client-card-window-title"></span>
            <span class="client-card-title-actions">
              <span class="client-card-status" style="color:var(--accent-green)"></span>
              <button type="button" class="window-card-button focus-client-window" title="Показать это окно игры" aria-label="Показать окно игры">👁</button>
            </span>
          </div>
          <div class="client-card-detail">Сервер: <span class="client-card-endpoint"></span> · Пинг: <b class="client-card-ping"></b></div>
          <div class="client-card-detail client-card-local"></div>
        `;
        card.addEventListener("click", () => window.selectProcessPid(pid));
        card.addEventListener("keydown", event => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            window.selectProcessPid(pid);
          }
        });
        card.querySelector(".focus-client-window").addEventListener("click", event => {
          event.stopPropagation();
          window.focusProcessWindow(pid);
        });
        clientCards.set(pid, card);
        grid.appendChild(card);
      }

      const pingText = instance.ping_ms !== null && instance.ping_ms !== undefined
        ? `${Math.round(instance.ping_ms)} мс (${instance.ping_type || 'сеть'})`
        : "измеряется...";
      const isSelected = pid === Number(selectedPid);
      card.style.order = String(index);
      card.classList.toggle("selected", isSelected);
      card.setAttribute("aria-pressed", isSelected ? "true" : "false");
      card.setAttribute("aria-label", `Выбрать ${instance.title || `игровое окно PID ${pid}`}`);
      card.querySelector(".client-card-window-title").textContent = instance.title || `Окно PID ${pid}`;
      card.querySelector(".client-card-status").textContent = `● ${instance.status_badge || instance.state || 'ЗАПУЩЕН'}`;
      card.querySelector(".client-card-endpoint").textContent = `${instance.remote_ip || '--'}:${instance.remote_port ?? '--'}`;
      card.querySelector(".client-card-ping").textContent = pingText;
      card.querySelector(".client-card-local").textContent = `Локальный порт: ${instance.local_port ?? '--'} · PID: ${pid}`;
    });
  }

  function renderWindowCharts(multiClient, selectedPid) {
    const section = document.getElementById("windowChartsSection");
    const grid = document.getElementById("windowChartsGrid");
    const windows = multiClient.windows || [];
    const endpoints = multiClient.endpoints || {};
    const shouldShow = interfacePreferences.windowCharts && windows.length > 0;
    section.style.display = shouldShow ? "block" : "none";
    document.getElementById("windowChartsCount").textContent = `Окон: ${windows.length}`;

    if (!shouldShow) {
      for (const chart of windowCharts.values()) chart.destroy();
      windowCharts.clear();
      grid.replaceChildren();
      return;
    }

    const activeIds = new Set(windows.map(item => item.window_id));
    for (const [windowId, chart] of windowCharts.entries()) {
      if (!activeIds.has(windowId)) {
        chart.destroy();
        windowCharts.delete(windowId);
        document.querySelector(`[data-window-chart-id="${windowId}"]`)?.remove();
      }
    }

    windows.forEach(item => {
      const windowId = item.window_id;
      let card = grid.querySelector(`[data-window-chart-id="${windowId}"]`);
      if (!card) {
        card = document.createElement("article");
        card.className = "window-chart-card";
        card.dataset.windowChartId = windowId;
        card.innerHTML = `
          <div class="window-chart-head">
            <div><div class="window-chart-title"></div><div class="window-chart-endpoint"></div></div>
            <div class="window-chart-side">
              <div class="window-chart-metrics"></div>
              <div class="window-chart-actions">
                <button type="button" class="window-card-button focus-game-window" title="Показать это окно игры" aria-label="Показать окно игры">👁</button>
                <button type="button" class="window-card-button toggle-window-chart" title="Свернуть мини-график" aria-label="Свернуть мини-график">▾</button>
              </div>
            </div>
          </div>
          <div class="window-chart-wrapper"><canvas></canvas></div>
          <div class="window-chart-empty" hidden>Ждём игровое TCP-соединение для этого окна…</div>
        `;
        grid.appendChild(card);
        windowCharts.set(windowId, createWindowChart(card.querySelector("canvas")));
        card.querySelector(".focus-game-window").addEventListener("click", () => focusProcessWindow(item.pid));
        card.querySelector(".toggle-window-chart").addEventListener("click", () => {
          if (collapsedWindowCharts.has(windowId)) collapsedWindowCharts.delete(windowId);
          else collapsedWindowCharts.add(windowId);
          renderWindowCharts(latestMultiClient, currentSelectedPid());
        });
      } else {
        grid.appendChild(card);
      }

      const endpoint = endpoints[item.endpoint_id];
      const ping = endpoint?.ping || {};
      const tcp = endpoint?.tcp_ping || {};
      card.classList.toggle("selected", Number(item.pid) === Number(selectedPid));
      card.querySelector(".window-chart-title").textContent = item.title || `Окно PID ${item.pid}`;
      card.querySelector(".window-chart-endpoint").textContent = endpoint
        ? `Зеркало ${item.endpoint_id}`
        : "Игровое соединение ещё не определено";
      card.querySelector(".window-chart-metrics").textContent = endpoint
        ? `ICMP ${ping.last_rtt != null ? Math.round(ping.last_rtt) + ' мс' : 'потеря'} · TCP ${tcp.last_rtt != null ? Math.round(tcp.last_rtt) + ' мс' : '—'}`
        : "Ожидание";
      const isCollapsed = collapsedWindowCharts.has(windowId);
      const toggleButton = card.querySelector(".toggle-window-chart");
      toggleButton.textContent = isCollapsed ? "▸" : "▾";
      toggleButton.title = isCollapsed ? "Развернуть мини-график" : "Свернуть мини-график";
      toggleButton.setAttribute("aria-label", toggleButton.title);
      card.classList.toggle("collapsed", isCollapsed);
      card.querySelector(".window-chart-wrapper").hidden = !endpoint || isCollapsed;
      card.querySelector(".window-chart-empty").hidden = Boolean(endpoint) || isCollapsed;
      if (endpoint) updateWindowChart(windowCharts.get(windowId), endpoint.chart || {}, endpoint.target?.port);
    });
  }

  function setSelectOptions(select, items, emptyText) {
    const previousValue = select.value;
    if (!items || items.length === 0) {
      select.innerHTML = `<option value="">${escapeHtml(emptyText)}</option>`;
      return;
    }
    select.innerHTML = items.map(item =>
      `<option value="${escapeHtml(item.id)}" title="${escapeHtml(item.description || item.location || '')}">${escapeHtml(item.name)}</option>`
    ).join("");
    if (items.some(item => item.id === previousValue)) select.value = previousValue;
  }

  function optimizerModeName(record, optimizer) {
    if (!record) return "";
    if (record.mode === "direct") return "Прямой маршрут";
    if (record.mode === "dpi") {
      const profile = (optimizer.dpi?.profiles || []).find(item => item.id === record.profile);
      return profile ? `DPI: ${profile.name}` : `DPI: ${record.profile}`;
    }
    if (record.mode === "relay") {
      const relay = (optimizer.relay?.relays || []).find(item => item.id === record.profile);
      return relay ? `Релей: ${relay.name}` : `Релей: ${record.profile}`;
    }
    return record.profile || record.mode;
  }

  function updateOptimizer(optimizer) {
    if (!optimizer || !optimizer.enabled) return;
    const analysis = optimizer.analysis || {};
    const metrics = analysis.metrics || {};
    const dpi = optimizer.dpi || {};
    const relay = optimizer.relay || {};
    const calibration = optimizer.calibration || {};

    const badge = document.getElementById("optimizerMainBadge");
    const badgeLabels = { good: "СТАБИЛЬНО", warning: "ТРЕБУЕТ ВНИМАНИЯ", critical: "ПРОБЛЕМА", info: "АНАЛИЗ" };
    badge.textContent = badgeLabels[analysis.level] || "АНАЛИЗ";
    badge.className = `optimizer-badge ${analysis.level || 'info'}`;

    const icons = { stable: "✅", local: "🏠", dpi: "🧩", transit: "🌍", destination: "🖥️", collecting: "🔎" };
    document.getElementById("optimizerRouteIcon").textContent = icons[analysis.kind] || "🔎";
    document.getElementById("optimizerRouteTitle").textContent = analysis.title || "Анализ маршрута";
    document.getElementById("optimizerRouteMessage").textContent = analysis.message || "Собираем данные…";

    const targets = (optimizer.targets || []).join(", ") || "не определён";
    const ports = (optimizer.ports || []).join(", ") || "—";
    document.getElementById("optimizerTargets").textContent = `${targets} · TCP ${ports}`;
    document.getElementById("optimizerMedian").textContent = metrics.median_rtt !== null && metrics.median_rtt !== undefined ? `${metrics.median_rtt} мс` : "--";
    document.getElementById("optimizerP95").textContent = metrics.p95_rtt !== null && metrics.p95_rtt !== undefined ? `${metrics.p95_rtt} мс` : "--";
    document.getElementById("optimizerLoss").textContent = metrics.loss_pct !== undefined ? `${metrics.loss_pct}%` : "--";
    document.getElementById("optimizerScore").textContent = metrics.score !== undefined && metrics.score < 100000 ? metrics.score : "--";

    const profileSelect = document.getElementById("dpiProfileSelect");
    const profileSignature = JSON.stringify((dpi.profiles || []).map(item => [item.id, item.name]));
    if (profileSelect.dataset.signature !== profileSignature) {
      setSelectOptions(profileSelect, dpi.profiles || [], "Нет профилей");
      profileSelect.dataset.signature = profileSignature;
    }
    const dpiAvailability = !dpi.available
      ? "winws не найден — укажите папку zapret"
      : (!dpi.is_admin ? "Нужен запуск от администратора" : "winws найден, точечный режим готов");
    document.getElementById("dpiAvailability").textContent = dpiAvailability;
    document.getElementById("dpiStateText").textContent = dpi.active
      ? `Активен: ${dpi.active_profile_name || dpi.active_profile}`
      : (dpi.last_error ? `Ошибка: ${dpi.last_error}` : "Обход выключен");
    document.getElementById("dpiStateDot").className = `state-dot ${dpi.active ? 'active' : (dpi.last_error ? 'error' : '')}`;
    document.getElementById("btnStartDpi").disabled = !dpi.available || !dpi.is_admin || dpi.active || relay.active;
    document.getElementById("btnStopDpi").disabled = !dpi.active;

    const relaySelect = document.getElementById("relaySelect");
    const relaySignature = JSON.stringify((relay.relays || []).map(item => [item.id, item.name]));
    if (relaySelect.dataset.signature !== relaySignature) {
      setSelectOptions(relaySelect, relay.relays || [], "Нет доступных релеев");
      relaySelect.dataset.signature = relaySignature;
    }
    document.getElementById("relayAvailability").textContent = !relay.configured
      ? "Релеи не настроены в config.json"
      : (!relay.available ? "WireGuard для Windows не найден" : (!relay.is_admin ? "Нужен запуск от администратора" : "Релеи готовы"));
    document.getElementById("relayStateText").textContent = relay.active
      ? `Активен: ${relay.active_relay}`
      : (relay.last_error ? `Ошибка: ${relay.last_error}` : "Прямой маршрут");
    document.getElementById("relayStateDot").className = `state-dot ${relay.active ? 'active' : (relay.last_error ? 'error' : '')}`;
    relaySelect.disabled = !relay.available || !relay.configured || relay.active;
    document.getElementById("btnStartRelay").disabled = !relay.available || !relay.configured || !relay.is_admin || relay.active || dpi.active;
    document.getElementById("btnStopRelay").disabled = !relay.active;

    document.getElementById("calibrationInstruction").textContent = calibration.instruction || "Соберите замеры для сравнения режимов.";
    const best = calibration.best;
    document.getElementById("calibrationBest").textContent = best
      ? `Лучший: ${optimizerModeName(best, optimizer)} · оценка ${best.score}`
      : "Лучший режим пока не определён";
    document.getElementById("calibrationRecords").innerHTML = (calibration.records || []).map(record =>
      `<span class="calibration-chip" title="${escapeHtml(record.time_str || '')}">${escapeHtml(optimizerModeName(record, optimizer))}: P95 ${record.p95_rtt ?? '--'} мс · потери ${record.loss_pct ?? '--'}% · балл ${record.score ?? '--'}</span>`
    ).join("");
  }

  async function requestJson(path, options = {}) {
    const response = await fetch(path, options);
    const contentType = response.headers.get("content-type") || "";
    const responseText = await response.text();
    let result;

    if (!contentType.includes("application/json")) {
      throw new Error(`Сервер вернул HTTP ${response.status} вместо JSON. Перезапустите FW-NetPulse и обновите страницу.`);
    }
    try {
      result = responseText ? JSON.parse(responseText) : {};
    } catch (error) {
      throw new Error("Сервер вернул повреждённый JSON. Перезапустите FW-NetPulse.");
    }
    if (!response.ok || result.success === false) {
      throw new Error(result.error || `Ошибка HTTP ${response.status}`);
    }
    return result;
  }

  async function postJson(path, body = {}) {
    return requestJson(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
  }

  async function optimizerPost(path, body = {}) {
    return postJson(path, body);
  }

  function syncWindowTabs(containerId, windows, activeWindowId, onSelect) {
    const container = document.getElementById(containerId);
    const signature = JSON.stringify(windows.map(item => [item.window_id, item.index, item.pid, item.endpoint_id]));
    if (container.dataset.signature !== signature) {
      container.innerHTML = "";
      windows.forEach(item => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "window-tab";
        button.dataset.windowId = item.window_id;
        button.title = `${item.title || 'Игровое окно'} · ${item.endpoint_id || 'соединение определяется'}`;
        button.textContent = `Окно #${item.index || '?'} · PID ${item.pid}`;
        button.addEventListener("click", () => onSelect(item.window_id));
        container.appendChild(button);
      });
      container.dataset.signature = signature;
    }
    container.querySelectorAll(".window-tab").forEach(button => {
      const isActive = button.dataset.windowId === activeWindowId;
      button.classList.toggle("active", isActive);
      button.setAttribute("aria-selected", isActive ? "true" : "false");
    });
  }

  function renderMtrRoute(route, target) {
    const mtrTbody = document.getElementById("mtrTableBody");
    if (!route || route.length === 0) {
      mtrTbody.innerHTML = `<tr><td colspan="10" class="table-loading">Маршрут для выбранного окна ещё строится…</td></tr>`;
      return;
    }

    let html = "";
    route.forEach(hop => {
      const isDest = hop.ip === target.ip;
      let lossBadgeHtml = "";
      if (featureFlags.smartMtr && hop.is_rate_limited) {
        lossBadgeHtml = `<span class="loss-pill ratelimit" title="Ограничение диагностического ICMP на узле; игровой трафик может проходить нормально">${hop.loss_pct}% (Rate-Limit)</span>`;
      } else {
        const lossClass = hop.loss_pct === 0 ? "zero" : (hop.loss_pct < 5 ? "warn" : "bad");
        lossBadgeHtml = `<span class="loss-pill ${lossClass}">${hop.loss_pct}%</span>`;
      }
      const geoHtml = featureFlags.geoIp
        ? `<span class="geo-tag">${hop.flag || '🌐'} ${escapeHtml(hop.city || 'Магистраль')}</span>`
        : `<span class="geo-tag">🌐 Магистраль</span>`;
      html += `
        <tr>
          <td><span class="hop-badge ${isDest ? 'dest' : ''}">${hop.hop}</span></td>
          <td>${geoHtml}</td>
          <td class="host-col">${escapeHtml(hop.hostname || hop.ip)}</td>
          <td><span class="ip-mono">${escapeHtml(hop.ip)}</span></td>
          <td><span class="cat-tag">${escapeHtml(hop.category || 'Транзит')}</span></td>
          <td style="text-align:center">${lossBadgeHtml}</td>
          <td style="text-align:right" class="rtt-val">${hop.last_rtt !== null ? hop.last_rtt + ' мс' : '<span style="color:var(--accent-red)">* * *</span>'}</td>
          <td style="text-align:right" class="rtt-val">${hop.min_rtt !== null ? hop.min_rtt : '--'}</td>
          <td style="text-align:right" class="rtt-val">${hop.avg_rtt !== null ? hop.avg_rtt : '--'}</td>
          <td style="text-align:right" class="rtt-val">${hop.max_rtt !== null ? hop.max_rtt : '--'}</td>
        </tr>`;
    });
    mtrTbody.innerHTML = html;
  }

  function getWindowSelection(windowId) {
    const selectedWindow = (latestMultiClient.windows || []).find(item => item.window_id === windowId);
    const endpoint = selectedWindow ? (latestMultiClient.endpoints || {})[selectedWindow.endpoint_id] : null;
    return { selectedWindow, endpoint };
  }

  function renderSelectedMtr() {
    const { selectedWindow, endpoint } = getWindowSelection(activeMtrWindowId);
    if (selectedWindow && endpoint) {
      document.getElementById("mtrWindowContext").textContent = `${selectedWindow.title} · зеркало ${selectedWindow.endpoint_id}`;
      renderMtrRoute(endpoint.route || [], endpoint.target || {});
    } else if (selectedWindow) {
      document.getElementById("mtrWindowContext").textContent = `${selectedWindow.title} · ждём игровое соединение`;
      renderMtrRoute([], {});
    } else {
      document.getElementById("mtrWindowContext").textContent = "Общая цель мониторинга · игровое окно не выбрано";
      renderMtrRoute(latestFallbackRoute, latestFallbackTarget);
    }
  }

  function renderSelectedHistory() {
    const { selectedWindow, endpoint } = getWindowSelection(activeHistoryWindowId);
    const events = endpoint?.recent_freezes || (selectedWindow ? [] : latestFallbackFreezes);
    document.getElementById("historyWindowContext").textContent = selectedWindow
      ? `${selectedWindow.title} · события зеркала ${selectedWindow.endpoint_id || 'ещё не определены'}`
      : "Общая цель мониторинга · игровое окно не выбрано";
    document.getElementById("freezeCountBadge").textContent = `Событий: ${events.length}`;
    renderFreezeHistory(events);
  }

  function updateMultiClientPanels(multiClient, fallbackRoute, fallbackTarget, fallbackFreezes) {
    latestMultiClient = multiClient || { windows: [], endpoints: {} };
    latestFallbackRoute = fallbackRoute || [];
    latestFallbackTarget = fallbackTarget || {};
    latestFallbackFreezes = fallbackFreezes || [];
    const windows = latestMultiClient.windows || [];
    const windowIds = windows.map(item => item.window_id);
    const preferredId = windowIds.includes(latestMultiClient.active_window_id)
      ? latestMultiClient.active_window_id
      : windowIds[0];
    if (!windowIds.includes(activeMtrWindowId)) activeMtrWindowId = preferredId || null;
    if (!windowIds.includes(activeHistoryWindowId)) activeHistoryWindowId = preferredId || null;

    const mtrTabs = document.getElementById("mtrWindowTabs");
    const historyTabs = document.getElementById("historyWindowTabs");
    mtrTabs.style.display = windows.length ? "flex" : "none";
    historyTabs.style.display = windows.length ? "flex" : "none";
    syncWindowTabs("mtrWindowTabs", windows, activeMtrWindowId, windowId => {
      activeMtrWindowId = windowId;
      syncWindowTabs("mtrWindowTabs", windows, activeMtrWindowId, () => {});
      renderSelectedMtr();
    });
    syncWindowTabs("historyWindowTabs", windows, activeHistoryWindowId, windowId => {
      activeHistoryWindowId = windowId;
      syncWindowTabs("historyWindowTabs", windows, activeHistoryWindowId, () => {});
      renderSelectedHistory();
    });

    for (const [endpointId, endpoint] of Object.entries(latestMultiClient.endpoints || {})) {
      const latestTimestamp = Number(endpoint.recent_freezes?.[0]?.timestamp || 0);
      const previousTimestamp = endpointFreezeTimes.get(endpointId);
      if (previousTimestamp !== undefined && latestTimestamp > previousTimestamp) playLagChime();
      endpointFreezeTimes.set(endpointId, Math.max(previousTimestamp || 0, latestTimestamp));
    }
    if (windows.length === 0 && latestFallbackFreezes.length > 0) {
      const latestTimestamp = Number(latestFallbackFreezes[0].timestamp || 0);
      if (lastFreezeTime !== 0 && latestTimestamp > lastFreezeTime) playLagChime();
      lastFreezeTime = Math.max(lastFreezeTime, latestTimestamp);
    }
    renderSelectedMtr();
    renderSelectedHistory();
  }

  // 3. Telemetry Update Handler
  function updateTelemetry(data, force = false) {
    if (!data) return;
    if (!force && shouldPauseUiRefresh()) {
      pendingTelemetry = data;
      document.body.classList.toggle("copy-paused", hasSelectedDashboardText());
      return;
    }
    document.body.classList.remove("copy-paused");

    const ping = data.ping || {};
    const tcp = data.tcp_ping || {};
    const route = data.route || [];
    const proc = data.game_process || {};
    const diag = data.diagnostics || {};
    const target = data.target || {};
    const optimizer = data.optimizer || {};
    const multiClient = data.multi_client || { windows: [], endpoints: {} };
    const serverSelectedPid = proc.pid !== null && proc.pid !== undefined ? Number(proc.pid) : null;
    if (requestedSelectedPid !== null && serverSelectedPid === requestedSelectedPid) {
      requestedSelectedPid = null;
    }
    const displayedSelectedPid = requestedSelectedPid ?? serverSelectedPid;
    if (requestedSelectedPid !== null && (multiClient.windows || []).some(item => Number(item.pid) === requestedSelectedPid)) {
      multiClient.active_window_id = `pid-${requestedSelectedPid}`;
    }

    if (data.app?.version) {
      document.getElementById("appVersion").textContent = `v${data.app.version}`;
    }

    // Header Server Display
    document.getElementById("targetIpDisplay").textContent = `${target.ip || '51.77.68.91'}:${target.port || 29000}`;
    document.getElementById("footerTarget").textContent = target.ip || '51.77.68.91';

    // 1. ICMP Ping Card
    const currentPingElem = document.getElementById("currentPing");
    const minPingElem = document.getElementById("minPing");
    const avgPingElem = document.getElementById("avgPing");
    const maxPingElem = document.getElementById("maxPing");
    const pingBadge = document.getElementById("pingQualityBadge");

    if (ping.last_rtt !== null && ping.last_rtt !== undefined) {
      currentPingElem.textContent = Math.round(ping.last_rtt);
      if (ping.last_rtt < 60) {
        pingBadge.textContent = "ОТЛИЧНО";
        pingBadge.className = "metric-badge good";
      } else if (ping.last_rtt < 120) {
        pingBadge.textContent = "НОРМАЛЬНО";
        pingBadge.className = "metric-badge warn";
      } else {
        pingBadge.textContent = "ВЫСОКИЙ ПИНГ";
        pingBadge.className = "metric-badge bad";
      }
    } else {
      currentPingElem.textContent = "LOST";
      pingBadge.textContent = "ПОТЕРЯ";
      pingBadge.className = "metric-badge bad";
    }

    minPingElem.textContent = ping.min_rtt !== null ? Math.round(ping.min_rtt) : "--";
    avgPingElem.textContent = ping.avg_rtt !== null ? Math.round(ping.avg_rtt) : "--";
    maxPingElem.textContent = ping.max_rtt !== null ? Math.round(ping.max_rtt) : "--";

    // 2. TCP Game Port Ping Card
    if (featureFlags.tcpPing && tcp.last_rtt !== null && tcp.last_rtt !== undefined) {
      document.getElementById("tcpPingVal").textContent = Math.round(tcp.last_rtt);
      document.getElementById("tcpAvgVal").textContent = tcp.avg_rtt !== null ? Math.round(tcp.avg_rtt) : "--";
      document.getElementById("tcpLossVal").textContent = `${tcp.loss_pct || 0}%`;
    } else {
      document.getElementById("tcpPingVal").textContent = "--";
      document.getElementById("tcpAvgVal").textContent = "--";
      document.getElementById("tcpLossVal").textContent = "0.0%";
    }

    // 3. Jitter Card
    const jitter = ping.jitter || 0.0;
    document.getElementById("currentJitter").textContent = jitter.toFixed(1);
    const jitterBadge = document.getElementById("jitterQualityBadge");
    if (jitter < 5.0) {
      jitterBadge.textContent = "ИДЕАЛЬНО";
      jitterBadge.className = "metric-badge good";
    } else if (jitter < 15.0) {
      jitterBadge.textContent = "СТАБИЛЬНО";
      jitterBadge.className = "metric-badge good";
    } else {
      jitterBadge.textContent = "НЕСТАБИЛЬНО";
      jitterBadge.className = "metric-badge warn";
    }

    // 4. Packet Loss Card
    const rollingLoss = ping.rolling_loss_pct || 0.0;
    document.getElementById("rollingLoss").textContent = rollingLoss.toFixed(1);
    document.getElementById("rollingLossSub").textContent = `${rollingLoss.toFixed(1)}%`;
    document.getElementById("lostCount").textContent = ping.total_lost || 0;
    document.getElementById("sentCount").textContent = ping.total_sent || 0;

    const lossBadge = document.getElementById("lossBadge");
    if (rollingLoss === 0.0) {
      lossBadge.textContent = "0.0%";
      lossBadge.className = "metric-badge good";
    } else if (rollingLoss < 3.0) {
      lossBadge.textContent = `${rollingLoss.toFixed(1)}% (МИКРО)`;
      lossBadge.className = "metric-badge warn";
    } else {
      lossBadge.textContent = `${rollingLoss.toFixed(1)}% (ПОТЕРИ)`;
      lossBadge.className = "metric-badge bad";
    }

    // 5. Multi-Client Windows Detection
    const instances = proc.instances || [];
    const count = proc.instances_count || 0;
    document.getElementById("procCountDisplay").textContent = `${count} ${count === 1 ? 'окно' : (count < 5 && count > 0 ? 'окна' : 'окон')}`;
    document.getElementById("socketBadge").textContent = `ОКОН: ${count}`;
    document.getElementById("socketBadge").className = count > 0 ? "metric-badge good" : "metric-badge";

    if (proc.detected) {
      document.getElementById("procNameDisplay").textContent = proc.process_name + (proc.pid ? ` [PID ${proc.pid}]` : '');
      document.getElementById("procSocketDisplay").textContent = `${proc.remote_ip}:${proc.remote_port}`;
      document.getElementById("procStatusText").textContent = proc.status_text;
    } else {
      document.getElementById("procNameDisplay").textContent = "Не запущен";
      document.getElementById("procSocketDisplay").textContent = `${target.ip}:${target.port}`;
      document.getElementById("procStatusText").textContent = proc.status_text;
    }

    // Показываем карточки даже для одного окна: пользователь всегда видит найденный клиент и его пинг.
    const multiSec = document.getElementById("multiClientSection");
    renderClientCards(instances, displayedSelectedPid);
    if (featureFlags.multiClient && interfacePreferences.clientList && instances.length > 0) {
      multiSec.style.display = "block";
    } else {
      multiSec.style.display = "none";
    }
    renderWindowCharts(multiClient, displayedSelectedPid);

    // 6. Diagnostics Banner
    const diagBanner = document.getElementById("diagBanner");
    diagBanner.className = `diag-banner ${diag.level || 'info'}`;
    document.getElementById("diagTitle").textContent = diag.title || "Анализ сети...";
    document.getElementById("diagMessage").textContent = diag.message || "";
    document.getElementById("diagRec").textContent = diag.recommendation ? `💡 Рекомендация: ${diag.recommendation}` : "";
    document.getElementById("diagBadge").textContent = diag.level === "good" ? "ОТЛИЧНО" : (diag.level === "warning" ? "ВНИМАНИЕ" : (diag.level === "critical" ? "КРИТИЧЕСКИ" : "АНАЛИЗ"));
    document.getElementById("diagIcon").textContent = diag.level === "good" ? "⚡" : (diag.level === "warning" ? "⚠️" : (diag.level === "critical" ? "🚨" : "🔍"));
    updateOptimizer(optimizer);

    // 7. Update Real-time Chart. Сервер уже совместил ряды по времени и сгладил длинные интервалы.
    const chartData = data.chart || {};
    if (interfacePreferences.mainChart && pingChart && Array.isArray(chartData.labels)) {
      const labels = chartData.labels;
      const icmpData = chartData.icmp || [];
      const tcpData = chartData.tcp || [];
      const icmpLoss = chartData.icmp_loss || [];
      const tcpLoss = chartData.tcp_loss || [];
      const avgData = chartData.average || [];
      const spikeData = chartData.spikes || [];
      const signature = JSON.stringify([
        labels.length,
        labels[labels.length - 1],
        icmpData[icmpData.length - 1],
        tcpData[tcpData.length - 1],
        icmpLoss[icmpLoss.length - 1],
        tcpLoss[tcpLoss.length - 1],
        spikeData[spikeData.length - 1]
      ]);

      // Не перерисовываем canvas каждые 500 мс, если текущая временная корзина не изменилась.
      if (signature !== lastChartSignature) {
        lastChartSignature = signature;
        pingChart.data.labels = labels;
        pingChart.data.datasets[0].data = icmpData;
        pingChart.data.datasets[1].label = `TCP игровой порт (:${target.port || 29000})`;
        pingChart.data.datasets[1].data = featureFlags.tcpPing ? tcpData : [];
        pingChart.data.datasets[2].data = avgData;
        pingChart.data.datasets[3].data = spikeData;
        pingChart.data.datasets[4].lossFlags = icmpLoss;
        pingChart.data.datasets[4].data = buildLossOverlay(icmpData, icmpLoss);
        pingChart.data.datasets[5].lossFlags = tcpLoss;
        pingChart.data.datasets[5].data = featureFlags.tcpPing ? buildLossOverlay(tcpData, tcpLoss) : [];
        pingChart.update("none");
      }

      const labelsMap = { 60: "60 секунд", 300: "5 минут", 600: "10 минут", 900: "15 минут" };
      const bucket = chartData.bucket_seconds || 1;
      document.getElementById("chartTimeframeLabel").textContent = `Последние ${labelsMap[activeTimeframe]} · одна точка = ${bucket} с`;
    }

    // 8. Отдельные вкладки маршрута и журнала для каждого окна игры.
    const recentFreezes = data.recent_freezes || [];
    updateMultiClientPanels(multiClient, route, target, recentFreezes);
  }

  function renderFreezeHistory(events) {
    const tbody = document.getElementById("historyTableBody");
    if (!events || events.length === 0) {
      tbody.innerHTML = `<tr><td colspan="5" class="table-empty">Сетевых фризов не зафиксировано. Связь стабильна!</td></tr>`;
      return;
    }

    let html = "";
    events.slice(0, 15).forEach(ev => {
      html += `
        <tr>
          <td style="font-family:var(--font-mono); font-size:0.8rem; color:var(--text-muted);">${ev.datetime_str.split(' ')[1] || ev.datetime_str}</td>
          <td style="font-weight:600; color:var(--accent-amber);">${escapeHtml(ev.reason)}</td>
          <td style="text-align:right; font-family:var(--font-mono); font-weight:700; color:#fff;">${ev.peak_rtt ? Math.round(ev.peak_rtt) + ' ms' : 'Потеря'}</td>
          <td style="font-size:0.84rem; color:var(--text-muted);">${escapeHtml(ev.root_cause || 'Анализ')}</td>
          <td><span class="ip-mono">${ev.target_ip}</span></td>
        </tr>
      `;
    });
    tbody.innerHTML = html;
  }

  function escapeHtml(str) {
    if (!str) return "";
    return String(str).replace(/[&<>"']/g, m => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;'
    }[m]));
  }

  // 4. Timeframe Switcher
  const timeframeBtns = document.querySelectorAll(".btn-timeframe");
  timeframeBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      timeframeBtns.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      activeTimeframe = parseInt(btn.getAttribute("data-timeframe"));
      lastChartSignature = "";

      const labelsMap = { 60: "60 секунд", 300: "5 минут", 600: "10 минут", 900: "15 минут" };
      document.getElementById("chartTimeframeLabel").textContent = `Загрузка данных за ${labelsMap[activeTimeframe] || ''}…`;

      // Update chart options for density
      if (pingChart) {
        pingChart.options.scales.x.ticks.maxTicksLimit = activeTimeframe > 300 ? 8 : 12;
      }
    });
  });

  // 5. Select Specific Process PID
  window.selectProcessPid = async function(pid) {
    const selectedPid = Number(pid);
    if (!Number.isInteger(selectedPid) || selectedPid <= 0) return;
    requestedSelectedPid = selectedPid;
    markSelectedProcess(selectedPid);
    try {
      await postJson("/api/select_process", { pid: selectedPid });
      const data = await requestJson(`/api/status?timeframe=${activeTimeframe}`);
      updateTelemetry(data, true);
    } catch (e) {
      requestedSelectedPid = null;
      markSelectedProcess(currentSelectedPid());
      showNotice(e.message || "Не удалось выбрать игровое окно", true);
      console.warn("Select PID error:", e);
    }
  };

  window.focusProcessWindow = async function(pid) {
    try {
      const result = await postJson("/api/focus_process", { pid: pid });
      showNotice(`Показано игровое окно: ${result.title || 'PID ' + pid}`);
    } catch (error) {
      showNotice(error.message || "Не удалось показать игровое окно", true);
    }
  };

  // 6. Launch Real Native Desktop Floating HUD
  document.getElementById("btnLaunchHud").addEventListener("click", async () => {
    try {
      await postJson("/api/hud/launch");
    } catch (e) {
      // Если отдельный EXE отсутствует, открываем браузерный HUD.
      const w = 340, h = 180, left = window.screen.width - w - 40, top = 60;
      window.open("/overlay.html", "FWNetPulseHUD", `width=${w},height=${h},top=${top},left=${left},menubar=no,toolbar=no`);
    }
  });

  // 7. Settings Modal & TCP Tweaker
  function currentSelectedPid() {
    if (requestedSelectedPid !== null) return requestedSelectedPid;
    const active = (latestMultiClient.windows || []).find(item => item.window_id === latestMultiClient.active_window_id);
    return active?.pid ?? null;
  }

  function applyInterfacePreferences() {
    document.getElementById("mainChartSection").style.display = interfacePreferences.mainChart ? "block" : "none";
    document.getElementById("optimizerSection").style.display = interfacePreferences.optimizer ? "block" : "none";
    document.getElementById("mtrSection").style.display = interfacePreferences.mtr ? "block" : "none";
    document.getElementById("historySection").style.display = interfacePreferences.history ? "block" : "none";
    if (!interfacePreferences.clientList) document.getElementById("multiClientSection").style.display = "none";
    renderWindowCharts(latestMultiClient, currentSelectedPid());

    const bindings = {
      toggleMainChartVisibility: "mainChart",
      toggleWindowChartsVisibility: "windowCharts",
      toggleClientListVisibility: "clientList",
      toggleOptimizerVisibility: "optimizer",
      toggleMtrVisibility: "mtr",
      toggleHistoryVisibility: "history"
    };
    Object.entries(bindings).forEach(([elementId, preferenceName]) => {
      const checkbox = document.getElementById(elementId);
      if (checkbox) checkbox.checked = Boolean(interfacePreferences[preferenceName]);
    });
  }

  const settingsModal = document.getElementById("settingsModal");
  document.getElementById("btnOpenSettings").addEventListener("click", async () => {
    settingsModal.classList.add("active");
    checkTcpTweaksStatus();
  });
  document.getElementById("btnCloseSettings").addEventListener("click", () => settingsModal.classList.remove("active"));
  document.getElementById("btnSaveSettingsClose").addEventListener("click", () => settingsModal.classList.remove("active"));

  const interfaceToggleBindings = {
    toggleMainChartVisibility: "mainChart",
    toggleWindowChartsVisibility: "windowCharts",
    toggleClientListVisibility: "clientList",
    toggleOptimizerVisibility: "optimizer",
    toggleMtrVisibility: "mtr",
    toggleHistoryVisibility: "history"
  };
  Object.entries(interfaceToggleBindings).forEach(([elementId, preferenceName]) => {
    document.getElementById(elementId).addEventListener("change", event => {
      interfacePreferences[preferenceName] = event.target.checked;
      saveSettings();
      applyInterfacePreferences();
    });
  });
  document.getElementById("btnResetInterfaceSettings").addEventListener("click", () => {
    interfacePreferences = { ...defaultInterfacePreferences };
    saveSettings();
    applyInterfacePreferences();
    showNotice("Вид панели восстановлен");
  });

  // Check Registry Tweaks Status
  async function checkTcpTweaksStatus() {
    const dot = document.getElementById("tweakDot");
    const txt = document.getElementById("tweakStatusText");
    try {
      const d = await requestJson("/api/tweaks/status");
      txt.textContent = d.status_text;
      dot.className = d.is_optimized ? "dot active" : "dot";
    } catch (e) {
      txt.textContent = "Не удалось проверить статус";
    }
  }

  document.getElementById("btnApplyTweaks").addEventListener("click", async () => {
    try {
      const d = await postJson("/api/tweaks/apply");
      alert(d.message);
      checkTcpTweaksStatus();
    } catch (e) {
      alert("Ошибка применения: " + e);
    }
  });

  document.getElementById("btnRevertTweaks").addEventListener("click", async () => {
    try {
      const d = await postJson("/api/tweaks/revert");
      alert(d.message);
      checkTcpTweaksStatus();
    } catch (e) {
      alert("Ошибка сброса: " + e);
    }
  });

  // 8. Безопасное управление игровым маршрутом
  document.getElementById("btnStartDpi").addEventListener("click", async () => {
    const select = document.getElementById("dpiProfileSelect");
    const profileName = select.options[select.selectedIndex]?.textContent || select.value;
    const confirmed = window.confirm(
      `Включить профиль «${profileName}» только для найденного IP и TCP-портов игры?\n\n` +
      "После включения потребуется переподключиться к игровому серверу. При проблемах нажмите «Отключить»."
    );
    if (!confirmed) return;
    try {
      const result = await optimizerPost("/api/optimizer/dpi/start", { profile: select.value, confirm: true });
      alert(result.message);
    } catch (error) {
      alert("Не удалось включить обход: " + error.message);
    }
  });

  document.getElementById("btnStopDpi").addEventListener("click", async () => {
    try {
      const result = await optimizerPost("/api/optimizer/dpi/stop");
      alert(result.message);
    } catch (error) {
      alert("Не удалось отключить обход: " + error.message);
    }
  });

  document.getElementById("btnStartRelay").addEventListener("click", async () => {
    const select = document.getElementById("relaySelect");
    const relayName = select.options[select.selectedIndex]?.textContent || select.value;
    const confirmed = window.confirm(
      `Направить только игровой IP через релей «${relayName}»?\n\n` +
      "Соединение с игрой может кратковременно прерваться. Остальной интернет останется прямым."
    );
    if (!confirmed) return;
    try {
      const result = await optimizerPost("/api/optimizer/relay/start", { relay_id: select.value, confirm: true });
      alert(result.message);
    } catch (error) {
      alert("Не удалось включить релей: " + error.message);
    }
  });

  document.getElementById("btnStopRelay").addEventListener("click", async () => {
    try {
      const result = await optimizerPost("/api/optimizer/relay/stop");
      alert(result.message);
    } catch (error) {
      alert("Не удалось отключить релей: " + error.message);
    }
  });

  document.getElementById("btnCaptureCalibration").addEventListener("click", async () => {
    try {
      const result = await optimizerPost("/api/optimizer/calibration/capture");
      alert(`Замер сохранён. Оценка качества: ${result.record.score}. Чем меньше число, тем лучше.`);
    } catch (error) {
      alert("Не удалось сохранить замер: " + error.message);
    }
  });

  // Feature Checkboxes
  const featureToggleBindings = {
    toggleTcpPing: "tcpPing",
    toggleSmartMtr: "smartMtr",
    toggleGeoIp: "geoIp",
    toggleMultiClient: "multiClient"
  };
  Object.entries(featureToggleBindings).forEach(([elementId, featureName]) => {
    const checkbox = document.getElementById(elementId);
    checkbox.checked = Boolean(featureFlags[featureName]);
    checkbox.addEventListener("change", event => {
      featureFlags[featureName] = event.target.checked;
      saveSettings();
      if (featureName === "multiClient" && !event.target.checked) {
        document.getElementById("multiClientSection").style.display = "none";
      }
    });
  });

  // Audio Toggle
  const audioBtn = document.getElementById("audioToggleBtn");
  audioBtn.addEventListener("click", () => {
    soundEnabled = !soundEnabled;
    audioBtn.className = soundEnabled ? "audio-toggle" : "audio-toggle muted";
    audioBtn.querySelector("b").textContent = soundEnabled ? "ВКЛ" : "ВЫКЛ";
    if (soundEnabled) playLagChime();
  });

  // Target Change Modal
  const modal = document.getElementById("targetModal");
  const btnChange = document.getElementById("btnChangeTarget");
  const btnCancel = document.getElementById("btnCancelModal");
  const btnSave = document.getElementById("btnSaveTarget");
  const modalInput = document.getElementById("modalIpInput");

  btnChange.addEventListener("click", () => { modal.classList.add("active"); modalInput.focus(); });
  btnCancel.addEventListener("click", () => modal.classList.remove("active"));
  btnSave.addEventListener("click", async () => {
    const newIp = modalInput.value.trim();
    if (newIp) {
      try {
        await postJson("/api/target", { ip: newIp });
        modal.classList.remove("active");
      } catch (e) {
        alert("Ошибка смены целевого IP: " + e.message);
      }
    }
  });

  // Export buttons
  document.getElementById("btnExportCsv").addEventListener("click", () => window.location.href = "/api/export/csv");
  document.getElementById("btnExportJson").addEventListener("click", () => window.location.href = "/api/export/json");

  // Polling loop with timeframe parameter
  async function pollTelemetry() {
    try {
      const data = await requestJson(`/api/status?timeframe=${activeTimeframe}`);
      updateTelemetry(data);
    } catch (e) {
      console.warn("Poll telemetry error:", e);
    }
    setTimeout(pollTelemetry, 500);
  }

  // Bootstrap
  document.addEventListener("pointerdown", event => {
    if (event.button !== 0 || event.target.closest("button, input, select, textarea, a")) return;
    selectionPointerDown = true;
  }, true);
  document.addEventListener("pointerup", () => {
    selectionPointerDown = false;
    requestAnimationFrame(flushPendingTelemetry);
  }, true);
  document.addEventListener("selectionchange", () => {
    if (hasSelectedDashboardText()) {
      document.body.classList.add("copy-paused");
    } else if (!selectionPointerDown) {
      requestAnimationFrame(flushPendingTelemetry);
    }
  });
  applyInterfacePreferences();
  initChart();
  pollTelemetry();
});
