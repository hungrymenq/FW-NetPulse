const { chromium } = require("playwright-core");

// Проверяет исходный сценарий пользователя: одно обычное нажатие должно
// пережить фоновое обновление панели и отправить ровно один выбор окна.
(async () => {
  const baseUrl = process.env.FW_NETPULSE_TEST_URL || "http://127.0.0.1:8897/";
  const executablePath = process.env.FW_NETPULSE_CHROME || "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
  const browser = await chromium.launch({ executablePath, headless: true });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
  let selectionRequests = 0;
  const consoleErrors = [];

  page.on("console", message => {
    if (message.type() === "error") consoleErrors.push(message.text());
  });

  page.on("request", request => {
    if (request.method() === "POST" && request.url().endsWith("/api/select_process")) {
      selectionRequests += 1;
    }
  });

  try {
    await page.goto(baseUrl, { waitUntil: "domcontentloaded" });
    await page.locator(".client-card-item").nth(1).waitFor({ state: "visible" });
    await page.evaluate(() => {
      window.__clientClickEvents = [];
      for (const eventName of ["pointerdown", "pointerup", "click"]) {
        document.addEventListener(eventName, event => {
          window.__clientClickEvents.push({
            eventName,
            pid: event.target.closest?.(".client-card-item")?.dataset.clientPid || null,
            target: event.target.className || event.target.tagName
          });
        }, true);
      }
    });
    const card = page.locator(".client-card-item").nth(1);
    const expectedPid = await card.getAttribute("data-client-pid");
    const expectedEndpoint = await card.locator(".client-card-endpoint").textContent();
    const box = await card.boundingBox();
    if (!box) throw new Error("Вторая карточка игрового окна не отображается");

    // За время удержания проходит хотя бы одно обновление с интервалом 500 мс.
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.waitForTimeout(750);
    const falseCopyNotice = await page.evaluate(() => document.body.classList.contains("copy-paused"));
    await page.mouse.up();
    await page.waitForTimeout(80);
    const selectedPid = await page.locator(".client-card-item.selected").getAttribute("data-client-pid");
    let backendState = { pid: "", endpoint: "" };
    for (let attempt = 0; attempt < 20; attempt += 1) {
      backendState = await page.evaluate(async () => {
        const response = await fetch("/api/status?timeframe=60");
        const status = await response.json();
        return {
          pid: String(status.game_process?.pid ?? ""),
          endpoint: `${status.target?.ip}:${status.target?.port}`
        };
      });
      if (backendState.pid === expectedPid && backendState.endpoint === expectedEndpoint) break;
      await page.waitForTimeout(200);
    }
    const clickEvents = await page.evaluate(() => window.__clientClickEvents);

    const errors = [];
    if (falseCopyNotice) errors.push("обычное нажатие ошибочно показало режим копирования текста");
    if (selectionRequests !== 1) {
      errors.push(`один клик отправил запросов выбора окна: ${selectionRequests}, ожидался 1`);
    }
    if (selectedPid !== expectedPid) {
      errors.push(`сразу выбрано окно PID ${selectedPid || 'нет'}, ожидался PID ${expectedPid}`);
    }
    if (backendState.pid !== expectedPid || backendState.endpoint !== expectedEndpoint) {
      errors.push(
        `API оставил PID ${backendState.pid || 'нет'} и цель ${backendState.endpoint}, ` +
        `ожидались PID ${expectedPid} и ${expectedEndpoint}`
      );
    }
    if (errors.length) {
      throw new Error(`${errors.join("; ")}; события: ${JSON.stringify(clickEvents)}; консоль: ${JSON.stringify(consoleErrors)}`);
    }

    const copiedText = await page.evaluate(() => {
      const node = document.querySelector(".client-card-item.selected .client-card-endpoint");
      const range = document.createRange();
      range.selectNodeContents(node);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      return selection.toString();
    });
    await page.waitForTimeout(750);
    const selectionState = await page.evaluate(() => ({
      text: window.getSelection().toString(),
      paused: document.body.classList.contains("copy-paused")
    }));
    if (selectionState.text !== copiedText || !selectionState.paused) {
      throw new Error("Выделенный адрес не пережил фоновое обновление панели");
    }
    await page.evaluate(() => window.getSelection().removeAllRanges());

    console.log("Один клик выбрал PID и цель API, а выделенный адрес пережил фоновое обновление.");
  } finally {
    await browser.close();
  }
})().catch(error => {
  console.error(error.message || error);
  process.exit(1);
});
