// FW-NetPulse Pro v2.0 Frontend Controller
document.addEventListener("DOMContentLoaded", () => {
  let pingChart = null;
  let soundEnabled = true;
  let audioCtx = null;
  let lastFreezeTime = 0;
  let activeTimeframe = 60; // 60s, 300s (5m), 600s (10m), 900s (15m)
  let lastChartSignature = "";

  // Feature Toggles state
  let featureFlags = {
    tcpPing: true,
    smartMtr: true,
    geoIp: true,
    multiClient: true
  };

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

  // 3. Telemetry Update Handler
  function updateTelemetry(data) {
    if (!data) return;

    const ping = data.ping || {};
    const tcp = data.tcp_ping || {};
    const route = data.route || [];
    const proc = data.game_process || {};
    const diag = data.diagnostics || {};
    const target = data.target || {};
    const optimizer = data.optimizer || {};

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
    const grid = document.getElementById("clientsGrid");
    if (featureFlags.multiClient && instances.length > 0) {
      multiSec.style.display = "block";
      grid.innerHTML = instances.map(inst => {
        const pingText = inst.ping_ms !== null && inst.ping_ms !== undefined
          ? `${Math.round(inst.ping_ms)} мс (${escapeHtml(inst.ping_type || 'сеть')})`
          : "измеряется...";
        return `
        <div class="client-card-item ${inst.pid === proc.pid ? 'selected' : ''}" onclick="selectProcessPid(${Number(inst.pid)})">
          <div class="client-card-title">
            <span>${escapeHtml(inst.title || `Окно PID ${inst.pid}`)}</span>
            <span style="color:var(--accent-green)">● ${escapeHtml(inst.status_badge || inst.state || 'ЗАПУЩЕН')}</span>
          </div>
          <div class="client-card-detail">Сервер: ${escapeHtml(inst.remote_ip || '--')}:${escapeHtml(inst.remote_port ?? '--')} · Пинг: <b>${pingText}</b></div>
          <div class="client-card-detail">Локальный порт: ${escapeHtml(inst.local_port ?? '--')} · PID: ${Number(inst.pid)}</div>
        </div>
      `;
      }).join("");
    } else {
      multiSec.style.display = "none";
    }

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
    if (pingChart && Array.isArray(chartData.labels)) {
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

    // 8. Update Smart MTR Table with GeoIP
    const mtrTbody = document.getElementById("mtrTableBody");
    if (route && route.length > 0) {
      let html = "";
      route.forEach(hop => {
        const isDest = hop.ip === target.ip;
        let lossBadgeHtml = "";

        if (featureFlags.smartMtr && hop.is_rate_limited) {
          lossBadgeHtml = `<span class="loss-pill ratelimit" title="Аппаратное ограничение ICMP на узле. Не влияет на игровой трафик">${hop.loss_pct}% (Rate-Limit)</span>`;
        } else {
          const lossClass = hop.loss_pct === 0 ? "zero" : (hop.loss_pct < 5 ? "warn" : "bad");
          lossBadgeHtml = `<span class="loss-pill ${lossClass}">${hop.loss_pct}%</span>`;
        }

        const geoHtml = featureFlags.geoIp ? `<span class="geo-tag">${hop.flag || '🌐'} ${escapeHtml(hop.city || 'Магистраль')}</span>` : `<span class="geo-tag">🌐 Магистраль</span>`;

        html += `
          <tr>
            <td><span class="hop-badge ${isDest ? 'dest' : ''}">${hop.hop}</span></td>
            <td>${geoHtml}</td>
            <td class="host-col">${escapeHtml(hop.hostname || hop.ip)}</td>
            <td><span class="ip-mono">${hop.ip}</span></td>
            <td><span class="cat-tag">${escapeHtml(hop.category || 'Транзит')}</span></td>
            <td style="text-align: center;">${lossBadgeHtml}</td>
            <td style="text-align: right;" class="rtt-val">${hop.last_rtt !== null ? hop.last_rtt + ' ms' : '<span style="color:var(--accent-red)">* * *</span>'}</td>
            <td style="text-align: right;" class="rtt-val">${hop.min_rtt !== null ? hop.min_rtt : '--'}</td>
            <td style="text-align: right;" class="rtt-val">${hop.avg_rtt !== null ? hop.avg_rtt : '--'}</td>
            <td style="text-align: right;" class="rtt-val">${hop.max_rtt !== null ? hop.max_rtt : '--'}</td>
          </tr>
        `;
      });
      mtrTbody.innerHTML = html;
    }

    // 9. Freeze Incident History
    const recentFreezes = data.recent_freezes || [];
    document.getElementById("freezeCountBadge").textContent = `Всего событий: ${recentFreezes.length}`;
    if (recentFreezes.length > 0) {
      const latest = recentFreezes[0];
      if (latest.timestamp > lastFreezeTime) {
        if (lastFreezeTime !== 0) playLagChime();
        lastFreezeTime = latest.timestamp;
      }
      renderFreezeHistory(recentFreezes);
    }
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
    try {
      await postJson("/api/select_process", { pid: pid });
    } catch (e) {
      console.warn("Select PID error:", e);
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
  const settingsModal = document.getElementById("settingsModal");
  document.getElementById("btnOpenSettings").addEventListener("click", async () => {
    settingsModal.classList.add("active");
    checkTcpTweaksStatus();
  });
  document.getElementById("btnCloseSettings").addEventListener("click", () => settingsModal.classList.remove("active"));
  document.getElementById("btnSaveSettingsClose").addEventListener("click", () => settingsModal.classList.remove("active"));

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
  document.getElementById("toggleTcpPing").addEventListener("change", (e) => featureFlags.tcpPing = e.target.checked);
  document.getElementById("toggleSmartMtr").addEventListener("change", (e) => featureFlags.smartMtr = e.target.checked);
  document.getElementById("toggleGeoIp").addEventListener("change", (e) => featureFlags.geoIp = e.target.checked);
  document.getElementById("toggleMultiClient").addEventListener("change", (e) => featureFlags.multiClient = e.target.checked);

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
  initChart();
  pollTelemetry();
});
