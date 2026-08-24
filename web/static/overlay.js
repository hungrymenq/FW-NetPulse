// In-Game Floating HUD Controller with Drag & Drop and Telemetry Stream
document.addEventListener("DOMContentLoaded", () => {
  const widget = document.getElementById("hudWidget");
  const header = document.getElementById("hudHeader");
  const settingsPanel = document.getElementById("hudSettingsPanel");
  const btnSettings = document.getElementById("btnHudSettings");
  const btnMini = document.getElementById("btnHudMini");
  const btnResetPos = document.getElementById("btnResetPos");

  const sliderOpacity = document.getElementById("sliderOpacity");
  const valOpacity = document.getElementById("valOpacity");
  const sliderScale = document.getElementById("sliderScale");
  const valScale = document.getElementById("valScale");

  const chkShowTcp = document.getElementById("chkShowTcp");
  const chkShowJitter = document.getElementById("chkShowJitter");
  const chkShowClients = document.getElementById("chkShowClients");

  const hudPing = document.getElementById("hudPing");
  const hudTcpPing = document.getElementById("hudTcpPing");
  const hudLoss = document.getElementById("hudLoss");
  const hudJitter = document.getElementById("hudJitter");
  const hudClientCount = document.getElementById("hudClientCount");
  const hudClientSelect = document.getElementById("hudClientSelect");

  // 1. DRAG AND DROP HANDLER
  let isDragging = false;
  let offsetX = 0;
  let offsetY = 0;

  // Restore saved position
  const savedLeft = localStorage.getItem("fw_hud_left");
  const savedTop = localStorage.getItem("fw_hud_top");
  if (savedLeft && savedTop) {
    widget.style.left = savedLeft + "px";
    widget.style.top = savedTop + "px";
  }

  header.addEventListener("mousedown", (e) => {
    isDragging = true;
    offsetX = e.clientX - widget.getBoundingClientRect().left;
    offsetY = e.clientY - widget.getBoundingClientRect().top;
    widget.style.transition = "none";
  });

  document.addEventListener("mousemove", (e) => {
    if (!isDragging) return;
    let newX = e.clientX - offsetX;
    let newY = e.clientY - offsetY;

    // Constrain to viewport
    newX = Math.max(0, Math.min(window.innerWidth - widget.offsetWidth, newX));
    newY = Math.max(0, Math.min(window.innerHeight - widget.offsetHeight, newY));

    widget.style.left = newX + "px";
    widget.style.top = newY + "px";
  });

  document.addEventListener("mouseup", () => {
    if (isDragging) {
      isDragging = false;
      localStorage.setItem("fw_hud_left", parseInt(widget.style.left));
      localStorage.setItem("fw_hud_top", parseInt(widget.style.top));
    }
  });

  btnResetPos.addEventListener("click", () => {
    widget.style.left = "30px";
    widget.style.top = "30px";
    localStorage.removeItem("fw_hud_left");
    localStorage.removeItem("fw_hud_top");
  });

  // 2. SETTINGS & CUSTOMIZATION
  btnSettings.addEventListener("click", () => {
    settingsPanel.classList.toggle("active");
  });

  btnMini.addEventListener("click", () => {
    widget.classList.toggle("mini");
  });

  // Opacity slider
  sliderOpacity.addEventListener("input", (e) => {
    const val = e.target.value;
    valOpacity.textContent = val + "%";
    widget.style.backgroundColor = `rgba(11, 15, 25, ${val / 100})`;
    localStorage.setItem("fw_hud_opacity", val);
  });

  const savedOpacity = localStorage.getItem("fw_hud_opacity");
  if (savedOpacity) {
    sliderOpacity.value = savedOpacity;
    valOpacity.textContent = savedOpacity + "%";
    widget.style.backgroundColor = `rgba(11, 15, 25, ${savedOpacity / 100})`;
  }

  // Scale slider
  sliderScale.addEventListener("input", (e) => {
    const val = e.target.value;
    valScale.textContent = val + "%";
    widget.style.transform = `scale(${val / 100})`;
    widget.style.transformOrigin = "top left";
    localStorage.setItem("fw_hud_scale", val);
  });

  const savedScale = localStorage.getItem("fw_hud_scale");
  if (savedScale) {
    sliderScale.value = savedScale;
    valScale.textContent = savedScale + "%";
    widget.style.transform = `scale(${savedScale / 100})`;
    widget.style.transformOrigin = "top left";
  }

  // Toggles
  chkShowTcp.addEventListener("change", (e) => {
    document.getElementById("hudTcpBox").style.display = e.target.checked ? "block" : "none";
  });
  chkShowJitter.addEventListener("change", (e) => {
    document.getElementById("hudJitterBox").style.display = e.target.checked ? "block" : "none";
  });
  chkShowClients.addEventListener("change", (e) => {
    document.getElementById("hudClientBar").style.display = e.target.checked ? "flex" : "none";
  });

  // 3. TELEMETRY STREAM
  let knownProcesses = [];
  async function pollHudTelemetry() {
    try {
      const resp = await fetch("/api/status");
      if (resp.ok) {
        const data = await resp.json();
        const ping = data.ping || {};
        const tcp = data.tcp_ping || {};
        const proc = data.game_process || {};

        // ICMP Ping
        if (ping.last_rtt !== null && ping.last_rtt !== undefined) {
          hudPing.textContent = Math.round(ping.last_rtt);
          hudPing.className = "hud-val " + (ping.last_rtt < 60 ? "good" : (ping.last_rtt < 120 ? "warn" : "bad"));
        } else {
          hudPing.textContent = "LOST";
          hudPing.className = "hud-val bad";
        }

        // TCP Ping
        if (tcp.last_rtt !== null && tcp.last_rtt !== undefined) {
          hudTcpPing.textContent = Math.round(tcp.last_rtt);
          hudTcpPing.className = "hud-val " + (tcp.last_rtt < 60 ? "good" : (tcp.last_rtt < 120 ? "warn" : "bad"));
        } else {
          hudTcpPing.textContent = "--";
          hudTcpPing.className = "hud-val";
        }

        // Loss
        const loss = ping.rolling_loss_pct || 0.0;
        hudLoss.textContent = loss.toFixed(1) + "%";
        hudLoss.className = "hud-val " + (loss === 0 ? "good" : (loss < 4 ? "warn" : "bad"));

        // Jitter
        const jitter = ping.jitter || 0.0;
        hudJitter.textContent = jitter.toFixed(1);
        hudJitter.className = "hud-val " + (jitter < 5 ? "good" : (jitter < 15 ? "warn" : "bad"));

        // Dynamic Multi-Client detection
        const instances = proc.instances || [];
        const count = proc.instances_count || 0;
        hudClientCount.textContent = `Окон: ${count}`;

        // Sync dropdown options if count changed
        const currentPids = instances.map(i => i.pid).join(",");
        if (currentPids !== knownProcesses.join(",")) {
          knownProcesses = instances.map(i => i.pid);
          hudClientSelect.innerHTML = `<option value="all">Все окна (${count})</option>`;
          instances.forEach(inst => {
            const opt = document.createElement("option");
            opt.value = inst.pid;
            opt.textContent = `Окно #${inst.index} (PID ${inst.pid}) -> :${inst.remote_port}`;
            hudClientSelect.appendChild(opt);
          });
        }
      }
    } catch (e) {
      console.warn("HUD Poll error:", e);
    }
    setTimeout(pollHudTelemetry, 500);
  }

  pollHudTelemetry();
});
