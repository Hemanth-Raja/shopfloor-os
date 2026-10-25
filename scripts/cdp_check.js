// tiny CDP driver: node --experimental-websocket cdp.js <url> <steps.json>
const [,, url, stepsFile] = process.argv;
const steps = JSON.parse(require("fs").readFileSync(stepsFile, "utf8"));
(async () => {
  const t = await (await fetch("http://localhost:9333/json/new?" + encodeURIComponent(url), {method: "PUT"})).json();
  const ws = new WebSocket(t.webSocketDebuggerUrl); let id = 0; const wait = {};
  ws.onmessage = m => { const d = JSON.parse(m.data); if (d.id && wait[d.id]) { wait[d.id](d); delete wait[d.id]; } };
  await new Promise(r => ws.onopen = r);
  const call = (method, params = {}) => new Promise(r => { const i = ++id; wait[i] = r; ws.send(JSON.stringify({id: i, method, params})); });
  await call("Runtime.enable");
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  for (const s of steps) {
    if (s.wait) await sleep(s.wait);
    if (s.js) { const r = await call("Runtime.evaluate", {expression: s.js, awaitPromise: true, returnByValue: true});
      console.log("JS>", JSON.stringify(r.result.result ? r.result.result.value : r.result).slice(0, 600)); }
    if (s.shot) { const r = await call("Page.captureScreenshot", {format: "png", captureBeyondViewport: false});
      require("fs").writeFileSync(s.shot, Buffer.from(r.result.data, "base64")); console.log("shot", s.shot); }
  }
  await fetch("http://localhost:9333/json/close/" + t.id); process.exit(0);
})();
