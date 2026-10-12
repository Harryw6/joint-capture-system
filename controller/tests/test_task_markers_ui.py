"""Task marker controls exercise the real JS against a mock HTTP boundary."""

from pathlib import Path
import subprocess


SCRIPT = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
class Element {
  constructor() { this.children = []; this.style = {}; this.dataset = {}; this.own = ''; this.listeners = {}; this.value = ''; }
  set textContent(value) { this.own = String(value); this.children = []; }
  get textContent() { return this.own + this.children.map(child => child.textContent || '').join(' '); }
  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  replaceChildren(...children) { this.own = ''; this.children = children; }
  addEventListener(event, callback) { (this.listeners[event] ||= []).push(callback); }
  fire(event) { return Promise.all((this.listeners[event] || []).map(callback => callback({preventDefault() {}}))); }
}
const nodes = new Map();
const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
get('task-marker-scope').value = 'auto';
const document = {getElementById: get, createElement: () => new Element(), querySelector: get, addEventListener() {}};
const state = {active:false, hosts:{}, allowed:{}, episode:null};
const nowMs = 1790000000123;
const startNs = '1789999990123456789';
const interval = {};
const calls = [];
let snapshot = {context:{key:'unitree:ongoing', episode_id:'ongoing', mode:'unitree', healthy:true}, current:null,
  segments:[], revision:0, local_directory:'C:/markers/ongoing', sync:{state:'pending'}};
let nextPost;
let failGet = false;
const clone = value => JSON.parse(JSON.stringify(value));
const reply = (status, data) => ({ok:status >= 200 && status < 300, status, json:async () => clone(data)});
const fetch = async (url, options = {}) => {
  calls.push({url, options});
  if (url === '/api/session') return reply(200, {token:'console-token'});
  if (url === '/api/state') return reply(200, state);
  if (url.startsWith('/api/task-markers?')) {
    if (failGet) throw new Error('offline');
    return reply(200, snapshot);
  }
  assert.equal(url, '/api/task-markers', 'marker controls must never submit capture actions');
  assert.equal(options.method, 'POST');
  assert.equal(options.headers['X-Console-Token'], 'console-token');
  assert.equal(options.headers['Content-Type'], 'application/json');
  const payload = JSON.parse(options.body);
  assert.match(payload.request_id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
  assert.equal(payload.client_wall_ms, nowMs);
  assert.equal(payload.context_key, snapshot.context.key);
  assert.equal(payload.expected_revision, snapshot.revision);
  return nextPost(payload);
};
const window = {addEventListener() {}};
class TestDate extends Date { static now() { return nowMs; } }
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {document, window, Date:TestDate,
  localStorage:{getItem:() => null}, fetch, AbortController,
  setTimeout:() => 0, clearTimeout() {}, setInterval:(callback, ms) => { interval[ms] = callback; }, console});
const flush = () => new Promise(resolve => setImmediate(resolve));
const poll = async () => { await interval[2000](); await flush(); };
const payloads = () => calls.filter(call => call.options.method === 'POST').map(call => JSON.parse(call.options.body));
(async () => {
  await flush();
  assert.equal(get('task-marker-start').disabled, false, 'Unitree context works without a joint manifest');
  assert.equal(get('task-marker-stop').disabled, true);
  assert.equal(get('task-marker-reset').disabled, true);
  assert.match(get('task-marker-context').textContent, /仅 Unitree.*ongoing/);
  let release;
  nextPost = async payload => {
    assert.equal(payload.action, 'start'); assert.equal(payload.scope, 'auto');
    await new Promise(resolve => { release = resolve; });
    snapshot.current = {id:'segment-1', index:1, state:'open', start_desktop_ns:startNs, end_desktop_ns:null,
      duration_ns:null, quality:'completed', reset_count:0, reset_history:[]};
    snapshot.segments = [clone(snapshot.current)]; snapshot.revision = 1;
    return reply(200, snapshot);
  };
  const starting = get('task-marker-start').fire('click');
  assert.equal(get('task-marker-start').disabled, true);
  assert.equal(get('task-marker-scope').disabled, true);
  assert.doesNotMatch(get('task-marker-feedback').textContent, /已记录/);
  await get('task-marker-start').fire('click');
  assert.equal(payloads().length, 1, 'in-flight click must not send another request');
  release(); await starting;
  assert.equal(get('task-marker-start').disabled, true);
  assert.equal(get('task-marker-stop').disabled, false);
  assert.equal(get('task-marker-reset').disabled, false);
  assert.equal(get('task-marker-duration').textContent, '00:00:09');
  assert.equal(get('task-marker-start-time').title, startNs + ' ns');
  const expectedTime = new Date(Number(BigInt(startNs) / 1000000n)).toLocaleString('zh-CN', {
    timeZone:'Asia/Shanghai', year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit',
    minute:'2-digit', second:'2-digit', fractionalSecondDigits:3, hour12:false});
  assert.equal(get('task-marker-start-time').textContent, expectedTime);

  snapshot.context.healthy = false; snapshot.context.reason = '相机状态过期'; await poll();
  assert.equal(get('task-marker-stop').disabled, false, 'an open segment remains closable when health is unknown');
  assert.equal(get('task-marker-reset').disabled, true);
  assert.match(get('task-marker-stop').textContent, /待核对/);
  assert.match(get('task-marker-hint').textContent, /相机状态过期/);

  snapshot.context.healthy = true; await poll();
  nextPost = async payload => {
    assert.equal(payload.action, 'reset');
    snapshot.current.reset_count = 1;
    snapshot.current.reset_history = [{start_desktop_ns:startNs, reset_desktop_ns:'1789999995123456789'}];
    snapshot.current.start_desktop_ns = '1789999995123456789';
    snapshot.segments = [clone(snapshot.current)]; snapshot.revision = 2;
    return reply(200, snapshot);
  };
  await get('task-marker-reset').fire('click');
  assert.match(get('task-marker-current').textContent, /重置 1 次/);
  assert.match(get('task-marker-feedback').textContent, /之前的开始点已保留/);
  assert.match(get('task-marker-history').textContent, /重置 1 次/);
  assert.match(get('task-marker-history').textContent, /此前开始.*重置于/);
  assert.ok(get('task-marker-history').textContent.includes(expectedTime), 'old start stays visible in reset history');

  nextPost = async () => {
    snapshot.revision = 3;
    return reply(409, {error:'记录版本已变化'});
  };
  await get('task-marker-stop').fire('click');
  assert.match(get('task-marker-feedback').textContent, /版本已变化/);
  assert.doesNotMatch(get('task-marker-feedback').textContent, /本段已结束/);
  assert.equal(get('task-marker-stop').disabled, false);

  nextPost = async payload => {
    assert.equal(payload.expected_revision, 3);
    const closed = {...snapshot.current, state:'closed', end_desktop_ns:'1790000000123456789',
      duration_ns:'5000000000', quality:'review_required'};
    snapshot.current = null; snapshot.segments = [closed]; snapshot.revision = 4;
    snapshot.sync = {state:'failed', error:'SSH unavailable'};
    return reply(200, snapshot);
  };
  await get('task-marker-stop').fire('click');
  assert.equal(get('task-marker-start').disabled, false);
  assert.equal(get('task-marker-stop').disabled, true);
  assert.match(get('task-marker-history').textContent, /已结束.*待核对/);
  assert.match(get('task-marker-sync').textContent, /同步失败.*SSH unavailable/);
  assert.equal(get('task-marker-duration').textContent, '00:00:05');
  assert.match(get('task-marker-feedback').textContent, /时间已记录/);

  snapshot.error = 'journal checksum mismatch'; snapshot.error_type = 'journal_unavailable';
  await poll();
  for (const action of ['start', 'stop', 'reset']) assert.equal(get(`task-marker-${action}`).disabled, true);
  assert.match(get('task-marker-badge').textContent, /日志无法读取/);
  assert.equal(get('task-marker-badge').className, 'pill amber');
  assert.match(get('task-marker-hint').textContent, /journal checksum mismatch/);
  assert.doesNotMatch(get('task-marker-sync').textContent, /已保存/);
  const beforeBlocked = payloads().length;
  await get('task-marker-start').fire('click');
  assert.equal(payloads().length, beforeBlocked, 'a corrupt journal must never accept a UI mutation');

  snapshot.current = {id:'segment-2', index:2, state:'open', start_desktop_ns:startNs, end_desktop_ns:null,
    duration_ns:null, quality:null, reset_count:0, reset_history:[]};
  snapshot.segments.push(clone(snapshot.current)); snapshot.revision = 5;
  snapshot.error = 'derived export cannot be written'; snapshot.error_type = 'export_failed';
  snapshot.sync = {state:'synced', clock:{unitree:{state:'unavailable', error:'probe timeout'}}};
  await poll();
  assert.equal(get('task-marker-start').disabled, true);
  assert.equal(get('task-marker-reset').disabled, true);
  assert.equal(get('task-marker-stop').disabled, false, 'durable journal plus failed export remains closable');
  assert.equal(get('task-marker-badge').className, 'pill amber');
  assert.match(get('task-marker-hint').textContent, /导出失败.*时钟映射待补齐/);
  assert.match(get('task-marker-clock').textContent, /时钟映射待补齐.*Unitree/);
  nextPost = async payload => {
    assert.equal(payload.action, 'stop');
    snapshot.segments[snapshot.segments.length - 1] = {...snapshot.current, state:'closed',
      end_desktop_ns:'1790000000123456789', duration_ns:'10000000000', quality:'completed'};
    snapshot.current = null; snapshot.revision = 6;
    return reply(200, snapshot);
  };
  await get('task-marker-stop').fire('click');
  assert.match(get('task-marker-feedback').textContent, /时间已写入本机日志，但导出\/同步失败/);
  assert.doesNotMatch(get('task-marker-feedback').textContent, /本段已结束，时间已记录/);
  assert.equal(get('task-marker-start').disabled, true);
  delete snapshot.error; delete snapshot.error_type;
  snapshot.sync = {state:'synced', clock:{unitree:{state:'estimated', estimated_error_ns:'1234567'}}};
  await poll();
  assert.equal(get('task-marker-start').disabled, false);
  assert.match(get('task-marker-sync').textContent, /已同步/);
  assert.match(get('task-marker-clock').textContent, /Unitree 1\.23 ms.*估计值/);
  assert.doesNotMatch(get('task-marker-hint').textContent, /时钟映射待补齐/);
  assert.doesNotMatch(get('task-marker-clock').textContent, /对时通过/);
  snapshot.segments[snapshot.segments.length - 1].duration_ns = '7000000000';
  await poll();
  assert.equal(get('task-marker-duration').textContent, '00:00:07', 'closed duration follows server monotonic duration across wall clock changes');
  const ids = payloads().map(payload => payload.request_id);
  assert.equal(new Set(ids).size, ids.length);

  get('task-marker-scope').value = 'joint';
  snapshot = {...snapshot, context:{key:'joint:next', episode_id:'next', mode:'joint', healthy:true}, segments:[], revision:0};
  await get('task-marker-scope').fire('change'); await flush();
  assert.equal(calls.filter(call => call.url.includes('task-markers?')).at(-1).url, '/api/task-markers?scope=joint');
  assert.match(get('task-marker-context').textContent, /联合采集.*next/);
  assert.match(get('task-marker-history').textContent, /尚无打点记录/);
  snapshot.context = {...snapshot.context, key:'joint:third', episode_id:'third'};
  await poll(); assert.match(get('task-marker-feedback').textContent, /当前采集已变化/);
  failGet = true; await poll();
  assert.equal(get('task-marker-start').disabled, true);
  assert.match(get('task-marker-badge').textContent, /连接未确认/);
  console.log('task marker payload, gating, exact timestamps, reset, conflict, storage errors, clock, scope, offline passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
"""


def test_task_marker_controls_use_scoped_api_and_confirmed_state():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["node", "-e", SCRIPT, str(root / "src/jointctl/console_web/app.js")],
        capture_output=True, text=True, encoding="utf-8", timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
