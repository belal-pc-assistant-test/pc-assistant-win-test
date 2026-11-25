"""The single-page viewer served to the approved browser. No external resources."""

VIEWER_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Remote view</title>
<style>
html,body{margin:0;height:100%;background:#111;color:#eee;font:14px system-ui,Segoe UI,sans-serif;overflow:hidden}
#bar{position:fixed;top:0;left:0;right:0;height:34px;background:#b00020;color:#fff;display:flex;flex-wrap:wrap;
gap:6px 16px;align-items:center;padding:0 12px;font-weight:600;font-size:13px;z-index:2;box-sizing:border-box}
#bar span{white-space:nowrap}
#stage{position:absolute;top:34px;bottom:26px;left:0;right:0;display:flex;align-items:center;justify-content:center;overflow:hidden}
#screen{display:block;max-width:100%;max-height:100%;width:auto;height:auto;cursor:crosshair;touch-action:none;background:#000}
#note{position:fixed;left:0;right:0;bottom:0;height:26px;line-height:26px;padding:0 12px;background:#222;font-size:12px;
color:#ccc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;box-sizing:border-box}
#ended{position:fixed;inset:0;display:none;align-items:center;justify-content:center;background:rgba(0,0,0,.85);
font-size:20px;text-align:center;padding:24px;box-sizing:border-box}
</style>
</head>
<body>
<div id="bar">
  <span>REMOTE VIEW + CONTROL</span>
  <span id="state">connecting…</span>
  <span id="fps">fps –</span>
  <span id="rtt">latency –</span>
  <span id="quality">quality –</span>
  <span id="left">time left –</span>
</div>
<div id="stage"><canvas id="screen" width="16" height="9" tabindex="0"></canvas></div>
<div id="note">Your mouse and keyboard control this PC while this page is open. Close the tab to stop viewing; the owner can end the session with /remote stop.</div>
<div id="ended"><div id="endedText">Session ended.</div></div>
<script>
(function () {
  'use strict';
  var canvas = document.getElementById('screen');
  var ctx = canvas.getContext('2d');
  var ws = null, live = false, ended = false;
  var framesThisSecond = 0, drawChain = Promise.resolve(), pings = {}, pingSeq = 0, lastMove = 0;
  function $(id) { return document.getElementById(id); }
  function clamp01(v) { return v < 0 ? 0 : (v > 1 ? 1 : v); }
  function setState(text) { $('state').textContent = text; }
  function formatLeft(seconds) {
    var s = Math.max(0, Math.floor(seconds)), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
    function two(n) { return (n < 10 ? '0' : '') + n; }
    return h ? h + ':' + two(m) + ':' + two(r) : two(m) + ':' + two(r);
  }
  function send(obj) {
    if (live && ws && ws.readyState === 1) { ws.send(JSON.stringify(obj)); }
  }
  function finish(text) {
    ended = true; live = false;
    setState('ended');
    $('endedText').textContent = text;
    $('ended').style.display = 'flex';
  }
  function drawFrame(blob) {
    return createImageBitmap(blob).then(function (bitmap) {
      if (canvas.width !== bitmap.width || canvas.height !== bitmap.height) {
        canvas.width = bitmap.width;
        canvas.height = bitmap.height;
      }
      ctx.drawImage(bitmap, 0, 0);
      bitmap.close();
    }).catch(function () { /* a damaged frame is simply skipped */ });
  }
  function handleText(text) {
    var msg;
    try { msg = JSON.parse(text); } catch (e) { return; }
    if (!msg || typeof msg !== 'object') { return; }
    if (msg.t === 'pong' && pings[msg.id] !== undefined) {
      $('rtt').textContent = 'latency ' + Math.round(performance.now() - pings[msg.id]) + ' ms';
      delete pings[msg.id];
    } else if (msg.t === 'stats') {
      $('quality').textContent = 'quality ' + msg.quality + ' · target ' + msg.fps_target + ' fps';
      $('left').textContent = 'time left ' + formatLeft(msg.remaining);
    } else if (msg.t === 'ended') {
      finish(String(msg.text || 'Session ended.').slice(0, 200));
    }
  }
  function connect() {
    var scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
    ws = new WebSocket(scheme + location.host + '/ws');
    ws.binaryType = 'blob';
    ws.onopen = function () { live = true; setState('connected'); };
    ws.onclose = function () {
      live = false;
      if (!ended) { setState('disconnected — reload this page with the same tab to reconnect'); }
    };
    ws.onmessage = function (event) {
      if (typeof event.data === 'string') { handleText(event.data); return; }
      framesThisSecond += 1;
      var blob = event.data;
      drawChain = drawChain.then(function () { return drawFrame(blob); });
    };
  }
  setInterval(function () {
    $('fps').textContent = 'fps ' + framesThisSecond;
    framesThisSecond = 0;
  }, 1000);
  setInterval(function () {
    if (live) {
      pingSeq += 1;
      pings[pingSeq] = performance.now();
      send({ t: 'ping', id: pingSeq });
    }
  }, 2000);

  function normalized(event) {
    var r = canvas.getBoundingClientRect();
    return { x: clamp01((event.clientX - r.left) / r.width), y: clamp01((event.clientY - r.top) / r.height) };
  }
  canvas.addEventListener('pointermove', function (event) {
    var now = performance.now();
    if (now - lastMove < 33) { return; }
    lastMove = now;
    var p = normalized(event);
    send({ k: 'move', x: p.x, y: p.y });
  });
  canvas.addEventListener('pointerdown', function (event) {
    event.preventDefault();
    canvas.focus();
    var p = normalized(event);
    send({ k: 'mdown', x: p.x, y: p.y, b: event.button });
  });
  canvas.addEventListener('pointerup', function (event) {
    event.preventDefault();
    var p = normalized(event);
    send({ k: 'mup', x: p.x, y: p.y, b: event.button });
  });
  canvas.addEventListener('contextmenu', function (event) { event.preventDefault(); });
  canvas.addEventListener('wheel', function (event) {
    event.preventDefault();
    send({ k: 'wheel', dy: event.deltaY });
  }, { passive: false });
  window.addEventListener('keydown', function (event) {
    if (!live) { return; }
    event.preventDefault();
    if (event.repeat) { return; }
    send({ k: 'key', down: true, key: event.key, code: event.code,
           ctrl: event.ctrlKey, alt: event.altKey, shift: event.shiftKey, meta: event.metaKey });
  }, true);
  window.addEventListener('keyup', function (event) {
    if (!live) { return; }
    event.preventDefault();
    send({ k: 'key', down: false, key: event.key, code: event.code,
           ctrl: event.ctrlKey, alt: event.altKey, shift: event.shiftKey, meta: event.metaKey });
  }, true);
  window.addEventListener('blur', function () { send({ k: 'releaseall' }); });
  connect();
})();
</script>
</body>
</html>
"""
