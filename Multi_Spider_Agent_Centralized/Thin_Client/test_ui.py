#!/usr/bin/env python3
"""Simple web UI to test PiCrawler rotation actions."""

import json
import httpx
from flask import Flask, Response

app = Flask(__name__)
API = "http://localhost:9004"

HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>PiCrawler Test</title>
<style>
  body { font-family: Arial; margin: 20px; background: #f5f5f5; }
  h2 { margin-top: 24px; }
  button { padding: 10px 20px; margin: 4px; font-size: 16px; cursor: pointer; border: 1px solid #ccc; border-radius: 6px; background: white; }
  button:hover { background: #e0e0e0; }
  button:active { background: #ccc; }
  .posture { background: #d4edda; }
  .turn { background: #cce5ff; }
  .angle { background: #fff3cd; }
  .look { background: #e2d9f3; }
  #log { margin-top: 16px; padding: 12px; background: #222; color: #0f0; font-family: monospace; font-size: 14px; height: 200px; overflow-y: auto; border-radius: 6px; white-space: pre-wrap; }
</style>
</head>
<body>
<h1>PiCrawler Test UI</h1>

<h2>Posture</h2>
<button class="posture" onclick="cmd('stand_up')">Stand Up</button>
<button class="posture" onclick="cmd('sit_down')">Sit Down</button>

<h2>Turn (normal)</h2>
<button class="turn" onclick="cmd('turn_left',{steps:1})">Turn Left x1</button>
<button class="turn" onclick="cmd('turn_left',{steps:2})">Turn Left x2</button>
<button class="turn" onclick="cmd('turn_left',{steps:3})">Turn Left x3</button>
<br>
<button class="turn" onclick="cmd('turn_right',{steps:1})">Turn Right x1</button>
<button class="turn" onclick="cmd('turn_right',{steps:2})">Turn Right x2</button>
<button class="turn" onclick="cmd('turn_right',{steps:3})">Turn Right x3</button>

<h2>Turn Angle — x1 step</h2>
<button class="angle" onclick="cmd('turn_left_angle',{steps:1,angle:10})">Left 10°</button>
<button class="angle" onclick="cmd('turn_left_angle',{steps:1,angle:15})">Left 15°</button>
<button class="angle" onclick="cmd('turn_left_angle',{steps:1,angle:20})">Left 20°</button>
<button class="angle" onclick="cmd('turn_left_angle',{steps:1,angle:25})">Left 25°</button>
<button class="angle" onclick="cmd('turn_left_angle',{steps:1,angle:30})">Left 30°</button>
<button class="angle" onclick="cmd('turn_left_angle',{steps:1,angle:35})">Left 35°</button>
<button class="angle" onclick="cmd('turn_left_angle',{steps:1,angle:40})">Left 40°</button>
<button class="angle" onclick="cmd('turn_left_angle',{steps:1,angle:45})">Left 45°</button>
<br>
<button class="angle" onclick="cmd('turn_right_angle',{steps:1,angle:10})">Right 10°</button>
<button class="angle" onclick="cmd('turn_right_angle',{steps:1,angle:15})">Right 15°</button>
<button class="angle" onclick="cmd('turn_right_angle',{steps:1,angle:20})">Right 20°</button>
<button class="angle" onclick="cmd('turn_right_angle',{steps:1,angle:25})">Right 25°</button>
<button class="angle" onclick="cmd('turn_right_angle',{steps:1,angle:30})">Right 30°</button>
<button class="angle" onclick="cmd('turn_right_angle',{steps:1,angle:35})">Right 35°</button>
<button class="angle" onclick="cmd('turn_right_angle',{steps:1,angle:40})">Right 40°</button>
<button class="angle" onclick="cmd('turn_right_angle',{steps:1,angle:45})">Right 45°</button>

<h2>Turn Angle — x2 steps</h2>
<button class="angle" onclick="cmd('turn_left_angle',{steps:2,angle:10})">Left 10° x2</button>
<button class="angle" onclick="cmd('turn_left_angle',{steps:2,angle:20})">Left 20° x2</button>
<button class="angle" onclick="cmd('turn_left_angle',{steps:2,angle:30})">Left 30° x2</button>
<button class="angle" onclick="cmd('turn_left_angle',{steps:2,angle:45})">Left 45° x2</button>
<br>
<button class="angle" onclick="cmd('turn_right_angle',{steps:2,angle:10})">Right 10° x2</button>
<button class="angle" onclick="cmd('turn_right_angle',{steps:2,angle:20})">Right 20° x2</button>
<button class="angle" onclick="cmd('turn_right_angle',{steps:2,angle:30})">Right 30° x2</button>
<button class="angle" onclick="cmd('turn_right_angle',{steps:2,angle:45})">Right 45° x2</button>

<h2>Look (tilt body, no movement)</h2>
<button class="look" onclick="cmd('look_left')">Look Left</button>
<button class="look" onclick="cmd('look_right')">Look Right</button>
<button class="look" onclick="cmd('look_up')">Look Up</button>
<button class="look" onclick="cmd('look_down')">Look Down</button>

<div id="log">Ready.\n</div>

<script>
async function cmd(action, params) {
  const log = document.getElementById('log');
  const body = {task_type: 'execute', action: action};
  if (params) body.params = params;

  const label = action + (params ? ' ' + JSON.stringify(params) : '');
  log.textContent += '> ' + label + '\\n';
  log.scrollTop = log.scrollHeight;

  try {
    const r = await fetch('/api/command', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
    const d = await r.json();
    log.textContent += '  ' + JSON.stringify(d) + '\\n';
  } catch(e) {
    log.textContent += '  ERROR: ' + e + '\\n';
  }
  log.scrollTop = log.scrollHeight;
}
</script>
</body>
</html>
"""

@app.route("/")
def index():
    return Response(HTML, mimetype="text/html")

@app.route("/api/command", methods=["POST"])
def proxy_command():
    from flask import request
    r = httpx.post(f"{API}/command", json=request.get_json(), timeout=15)
    return Response(r.content, mimetype="application/json")

if __name__ == "__main__":
    print("Test UI: http://0.0.0.0:8080")
    app.run(host="0.0.0.0", port=8080, debug=False)
