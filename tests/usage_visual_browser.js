// Compact Quotas layout regression. Uses Chrome DevTools Protocol directly so
// the test has no npm/browser-driver dependency.
const assert = require("node:assert/strict");
const {spawn} = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const [chromePath, baseUrl] = process.argv.slice(2);
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "devboost-usage-chrome-"));
const chrome = spawn(chromePath, ["--headless=new", "--remote-debugging-pipe",
  "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
  "--disable-component-update", "--disable-sync", "--disable-extensions", "--no-sandbox",
  `--user-data-dir=${profile}`, "about:blank"],
  {stdio: ["ignore", "pipe", "ignore", "pipe", "pipe"]});
let nextId = 0;
let sessionId;
let incoming = "";
const pending = new Map();
chrome.stdout.resume();
chrome.stdio[4].on("data", chunk => {
  incoming += chunk.toString();
  let end;
  while ((end = incoming.indexOf("\0")) >= 0) {
    const message = JSON.parse(incoming.slice(0, end));
    incoming = incoming.slice(end + 1);
    const request = pending.get(message.id);
    if (!request) continue;
    pending.delete(message.id);
    clearTimeout(request.timeout);
    if (message.error) request.reject(new Error(JSON.stringify(message.error)));
    else request.resolve(message.result);
  }
});
function send(method, params = {}, session = sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++nextId;
    const timeout = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Browser command timed out: ${method}`));
    }, 10000);
    pending.set(id, {resolve, reject, timeout});
    chrome.stdio[3].write(JSON.stringify({id, method, params, ...(session ? {sessionId: session} : {})}) + "\0");
  });
}
async function evaluate(expression) {
  const response = await send("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
  if (response.exceptionDetails) throw new Error(JSON.stringify(response.exceptionDetails));
  return response.result.value;
}
async function until(expression) {
  const deadline = Date.now() + 5000;
  while (Date.now() < deadline) {
    if (await evaluate(expression)) return;
    await new Promise(resolve => setTimeout(resolve, 25));
  }
  throw new Error(`Timed out waiting for ${expression}`);
}
async function main() {
  const target = await send("Target.createTarget", {url: "about:blank"}, null);
  const attached = await send("Target.attachToTarget", {targetId: target.targetId, flatten: true}, null);
  sessionId = attached.sessionId;
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", {width: 700, height: 1000, deviceScaleFactor: 1, mobile: false});
  await send("Page.navigate", {url: baseUrl + "/#usage"});
  await until("!!document.querySelector('#usage-body tr[data-usage-id] .usage-actions')");
  const result = await evaluate(`(() => {
    const card = document.querySelector('#page-usage .section-card').getBoundingClientRect();
    const table = document.querySelector('#page-usage table');
    const actions = document.querySelector('#usage-body tr[data-usage-id] .usage-actions').getBoundingClientRect();
    const controls = [...document.querySelectorAll('#usage-body tr[data-usage-id] .usage-actions button')].map(el => {
      const rect = el.getBoundingClientRect();
      return {label: el.getAttribute('aria-label'), width: rect.width, height: rect.height,
        left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom};
    });
    return {card: {left: card.left, right: card.right, top: card.top, bottom: card.bottom},
      tableWidth: table.getBoundingClientRect().width, tableScrollWidth: table.scrollWidth,
      actions: {left: actions.left, right: actions.right}, controls};
  })()`);
  assert.equal(result.controls.length, 3, "Quotas must render edit, remove, and drag controls");
  assert.equal(new Set(result.controls.map(control => control.label)).size, 3, "Quota controls need distinct accessible labels");
  assert.ok(result.tableScrollWidth <= result.tableWidth + 1, "Quotas table must not require horizontal scrolling");
  assert.ok(result.actions.left >= result.card.left, "Quota actions must stay inside the card on the left");
  assert.ok(result.actions.right <= result.card.right, "Quota actions must stay inside the card on the right");
  for (const control of result.controls) {
    assert.ok(control.width > 0 && control.height > 0, `${control.label} must be visible`);
    assert.ok(control.left >= result.card.left && control.right <= result.card.right,
      `${control.label} must remain visible inside the Quotas card`);
  }
}
main().catch(error => { console.error(error.stack || error); process.exitCode = 1; })
  .finally(() => chrome.kill());
