// Real browser input, no synthetic DragEvents and no npm/browser-driver dependencies.
// Run through: python3 -m unittest discover -s tests -p test_server_drag_browser.py -v
const assert = require("node:assert/strict");
const {spawn} = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const [chromePath, baseUrl, mode = "drag"] = process.argv.slice(2);
const profile = fs.mkdtempSync(path.join(os.tmpdir(), "devboost-chrome-"));
const chrome = spawn(chromePath, ["--headless=new", "--remote-debugging-pipe",
  "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
  "--disable-component-update", "--disable-sync", "--disable-extensions",
  "--no-sandbox", `--user-data-dir=${profile}`, "about:blank"],
  {stdio: ["ignore", "pipe", "pipe", "pipe", "pipe"]});
let nextId = 0;
let sessionId;
let incoming = "";
let chromeLog = "";
const pending = new Map();
chrome.stderr.on("data", chunk => { chromeLog = (chromeLog + chunk).slice(-4000); });
chrome.stdout.resume();
chrome.stdio[4].on("data", chunk => {
  incoming += chunk.toString();
  let end;
  while ((end = incoming.indexOf("\0")) >= 0) {
    const raw = incoming.slice(0, end);
    incoming = incoming.slice(end + 1);
    if (!raw) continue;
    const message = JSON.parse(raw);
    if (!pending.has(message.id)) continue;
    const {resolve, reject, timeout} = pending.get(message.id);
    pending.delete(message.id);
    clearTimeout(timeout);
    if (message.error) reject(new Error(JSON.stringify(message.error)));
    else resolve(message.result);
  }
});
function send(method, params = {}, session = sessionId) {
  return new Promise((resolve, reject) => {
    const id = ++nextId;
    const timeout = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Browser command timed out: ${method}\n${chromeLog}`));
    }, 10000);
    pending.set(id, {resolve, reject, timeout});
    chrome.stdio[3].write(JSON.stringify({id, method, params, ...(session ? {sessionId: session} : {})}) + "\0");
  });
}
const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
async function evaluate(expression) {
  const response = await send("Runtime.evaluate", {expression, returnByValue: true, awaitPromise: true});
  if (response.exceptionDetails) throw new Error(JSON.stringify(response.exceptionDetails));
  return response.result.value;
}
async function until(expression, message) {
  const deadline = Date.now() + 5000;
  do {
    if (await evaluate(expression)) return;
    await pause(25);
  } while (Date.now() < deadline);
  throw new Error(message + "; drag events: " + JSON.stringify(await evaluate("window.dragTrace")) +
    "; drop points: " + JSON.stringify(await evaluate("window.dropPoints")));
}
const selectorFor = (kind, id) => `${kind === "card" ? "#server-management-list .server-management-item" : "#tabs-bar .tab"}[data-server-id="${id}"]`;
const shadowFor = kind => kind === "card" ? ".server-card-drop-shadow" : ".server-tab-drop-shadow";
async function rect(selector) {
  const result = await evaluate(`(() => {
    const el = document.querySelector(${JSON.stringify(selector)});
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {x: r.x, y: r.y, width: r.width, height: r.height};
  })()`);
  assert.ok(result && result.width && result.height, `Visible element missing: ${selector}`);
  return result;
}
async function mouse(type, x, y, pressed = true) {
  await send("Input.dispatchMouseEvent", {type, x, y, button: pressed || type === "mouseReleased" ? "left" : "none",
    buttons: pressed ? 1 : 0, ...(type === "mouseMoved" ? {} : {clickCount: 1})});
  await pause(45);
}
async function beginDrag(kind, id) {
  const target = selectorFor(kind, id) + (kind === "card" ? " .server-drag-handle" : " .tab-name");
  const r = await rect(target);
  const start = {x: r.x + r.width / 2, y: r.y + r.height / 2};
  await mouse("mouseMoved", start.x, start.y, false);
  await mouse("mousePressed", start.x, start.y);
  await mouse("mouseMoved", start.x + 10, start.y + 5);
  await mouse("mouseMoved", start.x + 15, start.y + 10);
  await mouse("mouseMoved", start.x + 35, start.y + 20);
  assert.ok(await evaluate(`!!document.querySelector(${JSON.stringify(shadowFor(kind))})`),
    `${kind}: real mouse drag must keep a visible insertion placeholder; events: ` +
      JSON.stringify(await evaluate("window.dragTrace")));
}
async function dropAt(kind, targetId, before = false) {
  const r = await rect(selectorFor(kind, targetId));
  const targetX = before ? r.x + 8 : r.x + r.width - 8;
  const targetY = before ? r.y + 8 : r.y + r.height - 8;
  // The first move enters the target; another delivers dragover so its slot moves.
  await mouse("mouseMoved", targetX, targetY);
  await mouse("mouseMoved", targetX + (before ? 2 : -2), targetY + (before ? 2 : -2));
  await pause(150);
  // Release on the placeholder itself, which is the advertised drop target.
  const shadow = await rect(shadowFor(kind));
  const x = shadow.x + shadow.width / 2;
  const y = shadow.y + shadow.height / 2;
  await evaluate(`(window.dropPoints ||= []).push({target: ${JSON.stringify(r)}, shadow: ${JSON.stringify(shadow)},
    hit: document.elementFromPoint(${x}, ${y})?.className})`);
  await mouse("mouseMoved", x, y);
  await mouse("mouseMoved", x + 1, y + 1);
  await mouse("mouseReleased", x, y, false);
}
async function assertOrder(expected) {
  const json = JSON.stringify(expected);
  await until(`JSON.stringify([...document.querySelectorAll('#tabs-bar [data-server-id]')].map(el => el.dataset.serverId)) === ${JSON.stringify(json)}`,
    `Top tabs did not reach order ${json}`);
  assert.deepEqual(await evaluate("[...document.querySelectorAll('#server-management-list [data-server-id]')].map(el => el.dataset.serverId)"), expected);
  assert.deepEqual(await evaluate("fetch('/api/servers').then(r => r.json()).then(data => data.servers.map(s => s.id))"), expected,
    "Order must persist through the real API/configuration");
}
async function viewport(width) {
  await send("Emulation.setDeviceMetricsOverride", {width, height: 1400, deviceScaleFactor: 1, mobile: false});
  await pause(50);
}
async function main() {
  const target = await send("Target.createTarget", {url: "about:blank"}, null);
  const attached = await send("Target.attachToTarget", {targetId: target.targetId, flatten: true}, null);
  sessionId = attached.sessionId;
  await send("Page.enable");
  await send("Runtime.enable");
  await send("Emulation.setDeviceMetricsOverride", {width: 1280, height: 900, deviceScaleFactor: 1, mobile: false});
  await send("Page.addScriptToEvaluateOnNewDocument", {source: `
    window.dragTrace = [];
    window.reorderRequests = [];
    for (const name of ['dragstart', 'dragend', 'drop', 'dragover', 'mousedown', 'mouseup', 'mousemove']) document.addEventListener(name,
      event => window.dragTrace.push({type: name, trusted: event.isTrusted, target: event.target.className,
        x: event.clientX, y: event.clientY}), true);
    const originalFetch = window.fetch;
    window.fetch = function(url, init) {
      if (url === '/api/servers/reorder') {
        window.reorderRequests.push(JSON.parse(init.body).order);
        if (window.failNextReorder) {
          window.failNextReorder = false;
          return Promise.resolve(new Response(JSON.stringify({ok: false}), {status: 500}));
        }
      }
      if (window.holdNextStatus && String(url).startsWith('/api/status')) {
        window.holdNextStatus = false;
        return originalFetch.apply(this, arguments).then(response => new Promise(resolve => {
          window.releaseHeldStatus = () => resolve(response);
        }));
      }
      return originalFetch.apply(this, arguments);
    };
  `});
  await send("Page.navigate", {url: baseUrl + "/#servers"});
  if (mode === "empty") {
    await until("document.querySelector('#tabs-bar').textContent.includes('No servers yet.')", "Empty first-run tabs did not render");
    assert.ok(await evaluate("document.querySelector('#server-management-list').textContent.includes('No servers configured yet. Add an SSH connection to get started.')"),
      "Empty first-run server management state must explain the next step");
    assert.ok(await evaluate("document.querySelector('.server-manage-button').textContent.includes('Manage servers')"),
      "Empty first-run tabs must offer a path to server management");
    process.stdout.write("PASS: web first-run empty state, next-step guidance, and mobile-safe server navigation\n");
    return;
  }
  await until("document.querySelectorAll('#server-management-list [data-server-id]').length === 3", "Servers did not load");
  await assertOrder(["alpha", "beta", "gamma"]);
  await evaluate("window.holdNextStatus = true; window.heldStatusRequest = fetchStatus(); true");
  await until("typeof window.releaseHeldStatus === 'function'", "Pre-drag status response was not captured");
  await beginDrag("card", "alpha");
  await dropAt("card", "gamma");
  await until("window.reorderRequests.length === 1", "Card drag did not POST the order");
  await assertOrder(["beta", "gamma", "alpha"]);
  await evaluate("window.releaseHeldStatus(); window.heldStatusRequest");
  await assertOrder(["beta", "gamma", "alpha"]);
  await beginDrag("tab", "beta");
  // Exercise the same refresh paths used by the 4-second/15-second timers.
  await evaluate("Promise.all([fetchStatus(), fetchServers()])");
  assert.ok(await evaluate("!!document.querySelector('.server-tab-drop-shadow')"),
    "Background status/server refresh must preserve an active drag");
  await dropAt("tab", "alpha");
  await until("window.reorderRequests.length === 2", "Tab drag did not POST the order");
  await assertOrder(["gamma", "alpha", "beta"]);
  await beginDrag("card", "gamma");
  await send("Input.dispatchKeyEvent", {type: "keyDown", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27});
  await send("Input.dispatchKeyEvent", {type: "keyUp", key: "Escape", code: "Escape", windowsVirtualKeyCode: 27});
  await mouse("mouseReleased", 5, 5, false);
  await until("!document.querySelector('.server-card-drop-shadow')", "Escape must remove the drag placeholder");
  assert.equal(await evaluate("window.reorderRequests.length"), 2, "Cancelled drag must not save an order");
  await assertOrder(["gamma", "alpha", "beta"]);
  await viewport(820);
  assert.ok((await rect(selectorFor("card", "beta"))).y > (await rect(selectorFor("card", "gamma"))).y,
    "Wrapped-card case must span multiple rows");
  await beginDrag("card", "gamma");
  await dropAt("card", "beta");
  await assertOrder(["alpha", "beta", "gamma"]);
  await viewport(500);
  assert.equal((await rect(selectorFor("card", "alpha"))).x, (await rect(selectorFor("card", "beta"))).x,
    "Narrow-card case must use one column");
  await beginDrag("card", "gamma");
  await dropAt("card", "alpha", true);
  await assertOrder(["gamma", "alpha", "beta"]);
  await viewport(260);
  assert.ok((await rect(selectorFor("tab", "beta"))).y > (await rect(selectorFor("tab", "gamma"))).y,
    "Wrapped-tab case must span multiple rows");
  await beginDrag("tab", "beta");
  await dropAt("tab", "gamma", true);
  await assertOrder(["beta", "gamma", "alpha"]);
  await viewport(1280);
  await beginDrag("tab", "beta");
  const lastTab = await rect(selectorFor("tab", "alpha"));
  const manageButton = await rect(".server-manage-button");
  const gapX = (lastTab.x + lastTab.width + manageButton.x) / 2;
  const gapY = lastTab.y + lastTab.height / 2;
  await mouse("mouseMoved", gapX, gapY);
  await mouse("mouseMoved", gapX + 1, gapY);
  await mouse("mouseReleased", gapX, gapY, false);
  await assertOrder(["gamma", "alpha", "beta"]);
  assert.equal(await evaluate("window.reorderRequests.length"), 6, "Each completed drag saves exactly once");
  await evaluate("window.failNextReorder = true");
  await beginDrag("tab", "gamma");
  await dropAt("tab", "beta");
  await until("document.getElementById('toast').textContent.includes(\"Couldn't save server order\")", "Save failure must be visible");
  await assertOrder(["gamma", "alpha", "beta"]);
  assert.equal(await evaluate("window.reorderRequests.length"), 7, "Failed save was attempted");
  assert.equal(await evaluate("document.querySelector('#tabs-bar .active').dataset.serverId"), "alpha",
    "Dragging must preserve the selected server");
  assert.ok((await evaluate("window.dragTrace.filter(event => event.type === 'dragstart')")).every(event => event.trusted),
    "Regression must exercise browser-produced drag events");
  const betaTab = await rect(selectorFor("tab", "beta"));
  await mouse("mousePressed", betaTab.x + betaTab.width / 2, betaTab.y + betaTab.height / 2);
  await mouse("mouseReleased", betaTab.x + betaTab.width / 2, betaTab.y + betaTab.height / 2, false);
  assert.equal(await evaluate("document.querySelector('#tabs-bar .active').dataset.serverId"), "beta", "Normal tab clicks still select servers");
  await send("Page.reload", {ignoreCache: true});
  await until("document.querySelectorAll('#tabs-bar [data-server-id]').length === 3", "Reload did not load servers");
  await assertOrder(["gamma", "alpha", "beta"]);
  process.stdout.write("PASS: real card/tab dragging, wrapped/single-column layouts, placeholder/gap drops, stale polling, cancellation, save rollback, selection, and reload persistence\n");
}
main().catch(error => {
  process.stderr.write(error.stack + "\n");
  process.exitCode = 1;
}).finally(async () => {
  for (const {timeout} of pending.values()) clearTimeout(timeout);
  chrome.kill("SIGTERM");
  await new Promise(resolve => { if (chrome.exitCode !== null) resolve(); else chrome.once("exit", resolve); });
  fs.rmSync(profile, {recursive: true, force: true});
});
