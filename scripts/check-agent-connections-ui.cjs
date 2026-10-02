#!/usr/bin/env node
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { JSDOM } = process.env.JSDOM_PATH ? createRequire(path.resolve(process.env.JSDOM_PATH, 'package.json'))('jsdom') : require('jsdom');
const staticDir = path.resolve(__dirname, '../src/agent_commons/static');
const me = { id: 'human', name: 'Tim', handle: 'tim', kind: 'human', has_password: true };
const agent = { id: 'agent', name: 'Scout', handle: 'tim-scout', kind: 'agent', owner: me };
const other = { ...agent, id: 'other', name: 'Other agent', owner: { id: 'another', name: 'Another human' } };
const project = { id: 'project', name: 'Amplitude' };
const token = 'private-one-time-connection-token';
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
async function flush() { for (let i = 0; i < 5; i++) await tick(); }
const response = (data, status = 200) => ({ status, ok: status < 400, json: async () => structuredClone(data) });
async function setup() {
  const dom = new JSDOM(fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8'), { url: 'http://127.0.0.1:8002', runScripts: 'outside-only' }); const w = dom.window;
  w.Headers = Headers; w.crypto.randomUUID = require('node:crypto').randomUUID; w.setInterval = () => 1; w.clearInterval = () => {};
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; }; w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  const requests = []; const blobs = []; const downloads = []; const revokedURLs = [];
  w.URL.createObjectURL = blob => { blobs.push(blob); return `blob:credential-${blobs.length}`; }; w.URL.revokeObjectURL = url => revokedURLs.push(url); w.HTMLAnchorElement.prototype.click = function () { downloads.push({ href: this.href, filename: this.download }); };
  let connections = ['active', 'bound', 'new', 'revoked'].map((id, i) => ({ id, label: `<img src=x> session ${i}`, actor: agent, project, active: id === 'active', bound: ['active', 'bound'].includes(id), revoked: id === 'revoked', lease_expires_at: null, last_seen_at: null }));
  connections.push({ id: 'others-connection', label: 'Must not show', actor: other, project });
  let deferCreate = null;
  w.fetch = async (url, options = {}) => {
    const request = { url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null }; requests.push(request);
    if (url === '/v1/me') return response(me);
    if (url === '/v1/projects') return response({ items: [] });
    if (url.endsWith('/members')) return response({ items: [{ actor: me, role: 'guest' }, { actor: agent, role: 'member' }, { actor: other, role: 'member' }] });
    if (url === '/v1/actors') return response({ items: [me, agent, other] });
    if (url.startsWith('/v1/agent-connections?')) return response({ items: connections });
    if (url === '/v1/agent-connections' && request.method === 'POST') {
      const result = { connection: { id: 'created', label: request.body.label, actor: agent, project, bound: false, active: false, revoked: false, bound_session_id: 'never-expose-session-id' }, token };
      if (deferCreate) return deferCreate(result); connections.push(result.connection); return response(result, 201);
    }
    if (request.method === 'DELETE') { connections = connections.map(c => url.endsWith(`/${c.id}`) ? { ...c, revoked: true, active: false } : c); return response(null, 204); }
    return response({ items: [] });
  };
  const source = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8').replace('  const savedToken = state.token;', '  window.__connectionTest = { state, ui, showMembers, createAgentConnection };\n  const savedToken = state.token;'); w.eval(source); await flush(); const api = w.__connectionTest; api.state.project = project;
  return { w, requests, blobs, downloads, revokedURLs, ...api, defer: fn => { deferCreate = fn; } };
}
(async () => {
  const app = await setup(); const d = app.w.document; await app.showMembers();
  assert.equal(d.querySelectorAll('.member-row button').length, 1, 'guest can connect their owned agent, not another agent');
  assert.equal(d.querySelector('.member-row button').textContent, 'Connect session');
  assert.equal(d.querySelectorAll('.connection-row').length, 4, 'other humans connections remain private');
  assert.deepEqual([...d.querySelectorAll('.session-status')].map(el => el.textContent), ['Session active', 'Session bound, idle/offline', 'Not connected', 'Revoked']);
  assert.equal(d.querySelectorAll('.connection-row img').length, 0); assert.match(d.querySelector('#modal-content').textContent, /does not show whether the model is working/);
  d.querySelector('.member-row button').click(); const form = d.querySelector('#modal-content form'); form.querySelector('[name=label]').value = 'Tim on ws2, amplitude project'; form.dispatchEvent(new app.w.Event('submit', { bubbles: true, cancelable: true })); await flush();
  assert.deepEqual(app.requests.find(r => r.method === 'POST').body, { actor_id: 'agent', project_id: 'project', label: 'Tim on ws2, amplitude project' });
  assert.equal(d.querySelector('.connection-secret').textContent, token); assert.match(d.querySelector('#modal-content').textContent, /--settings/); assert.match(d.querySelector('#modal-content').textContent, /--url override/); assert.match(d.querySelector('#modal-content').textContent, /localhost browser tunnel/);
  const save = [...d.querySelectorAll('#modal-content button')].find(b => b.textContent === 'Save connection file'); save.click();
  const file = await new Promise((resolve, reject) => { const reader = new app.w.FileReader(); reader.onload = () => resolve(JSON.parse(reader.result)); reader.onerror = reject; reader.readAsText(app.blobs[0]); });
  assert.equal(file.url, 'http://127.0.0.1:8002'); assert.equal(file.token, token); assert.equal(file.connection.id, 'created'); assert.equal(file.connection.bound_session_id, undefined); assert.equal(app.downloads[0].filename, 'agent-connection-created.json'); assert.equal(app.downloads[0].href.includes(token), false);
  assert.equal([...Array(app.w.sessionStorage.length)].some((_, i) => app.w.sessionStorage.getItem(app.w.sessionStorage.key(i)).includes(token)), false);
  app.ui.modal.close(); assert.equal(d.querySelector('#modal-content').textContent.includes(token), false); const savedCount = app.blobs.length; save.click(); assert.equal(app.blobs.length, savedCount, 'closed credential cannot be downloaded again');
  await app.showMembers(); const revoke = d.querySelector('.connection-row button'); const revokedId = 'active'; revoke.click(); await flush(); assert.equal(app.requests.find(r => r.method === 'DELETE').url, `/v1/agent-connections/${revokedId}`); assert.equal(d.querySelector('.connection-row .session-status').textContent, 'Revoked');
  const staleRevoke = d.querySelector('.connection-row button'); const priorDeletes = app.requests.filter(r => r.method === 'DELETE').length; app.state.projectGeneration++; staleRevoke.click(); await flush(); assert.equal(app.requests.filter(r => r.method === 'DELETE').length, priorDeletes, 'old-project revoke action is ignored');
  app.state.members = app.state.members.filter(member => member.actor.id !== agent.id); const priorTitle = d.querySelector('#modal-title').textContent; app.createAgentConnection(agent, project.id); assert.equal(d.querySelector('#modal-title').textContent, priorTitle, 'owned agent must already be a project member');
  const priorPosts = app.requests.filter(r => r.method === 'POST').length; app.createAgentConnection(other, project.id); assert.equal(app.requests.filter(r => r.method === 'POST').length, priorPosts); app.state.me = { ...agent }; await app.showMembers(); assert.equal([...d.querySelectorAll('button')].some(b => b.textContent === 'Connect session'), false); app.w.close();
  const late = await setup(); await late.showMembers(); let release; late.defer(result => new Promise(resolve => { release = () => resolve(response(result, 201)); })); late.createAgentConnection(agent, project.id); const lateForm = late.w.document.querySelector('#modal-content form'); lateForm.querySelector('[name=label]').value = 'Delayed connection'; lateForm.dispatchEvent(new late.w.Event('submit', { bubbles: true, cancelable: true })); await flush(); late.state.projectGeneration++; late.state.project = { id: 'different', name: 'Different project' }; release(); await flush(); assert.equal(late.w.document.querySelector('.connection-secret'), null, 'late connection credential cannot replace the current project'); assert.equal(late.w.document.querySelector('#modal-content').textContent.includes(token), false); late.w.close();
  console.log('Agent connection DOM checks passed: owned member controls, guest ownership, private list/statuses, create/download/cleanup, revoke and navigation guards.');
})().catch(error => { console.error(error); process.exitCode = 1; });
