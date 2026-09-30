#!/usr/bin/env node
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { JSDOM } = process.env.JSDOM_PATH
  ? createRequire(path.resolve(process.env.JSDOM_PATH, 'package.json'))('jsdom') : require('jsdom');
const directory = path.resolve(__dirname, '../src/agent_commons/static');
const dom = new JSDOM(fs.readFileSync(path.join(directory, 'index.html'), 'utf8'), {
  url: 'https://workspace.test', runScripts: 'outside-only', pretendToBeVisual: true,
});
const w = dom.window; w.Headers = Headers;
w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
w.HTMLDialogElement.prototype.close = function () { this.open = false; };
w.setInterval = () => 1; w.clearInterval = () => {};
const source = fs.readFileSync(path.join(directory, 'app.js'), 'utf8');
w.eval(source.replace('  const savedToken = state.token;', '  window.__names = { state, ui, editName, updateNameControls, pollEvents };\n  const savedToken = state.token;'));
const { state, ui, editName, updateNameControls, pollEvents } = w.__names;
const me = { id: 'owner', name: 'Owner', kind: 'human' };
let project = { id: 'project', name: 'Original project', description: 'Project description' };
let channel = { id: 'channel', project_id: 'project', name: 'Original channel', description: 'Channel description' };
const requests = []; let events = []; let delayed;
w.fetch = async (url, options = {}) => {
  const body = options.body ? JSON.parse(options.body) : null;
  requests.push({ url, method: options.method || 'GET', body });
  if (options.method === 'PATCH' && delayed) return new Promise(resolve => { delayed.resolve = () => resolve({ ok: true, status: 200, json: async () => project }); });
  let data;
  if (url === '/v1/projects/project' && options.method === 'PATCH') { project = { ...project, ...body }; data = project; }
  else if (url === '/v1/channels/channel' && options.method === 'PATCH') { channel = { ...channel, ...body }; data = channel; }
  else if (url === '/v1/projects') data = { items: [project] };
  else if (url.endsWith('/channels')) data = { items: [channel] };
  else if (url.includes('/events')) data = { items: events, cursor: 2, next_cursor: null };
  else data = { items: [] };
  return { ok: true, status: 200, json: async () => structuredClone(data) };
};
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
async function flush() { for (let i = 0; i < 6; i++) await tick(); }
function submit(value) { w.document.querySelector('#modal-content [name=name]').value = value; w.document.querySelector('#modal-content form').dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true })); }
(async () => {
  state.me = me; state.token = 'owner-token'; state.project = project; state.projects = [project]; state.channel = channel; state.channels = [channel]; state.members = [{ actor: me, role: 'owner' }];
  state.thread = { id: 'thread' }; state.messages = [{ id: 'message', text: 'Keep message' }]; state.messageCursor = 7; state.replyTo = { id: 'message' }; ui.input.value = 'Keep draft';
  updateNameControls(); assert.equal(w.document.querySelector('#edit-project').hidden, false); assert.equal(w.document.querySelector('#edit-channel').hidden, false);
  editName('project'); assert.equal(w.document.querySelector('#modal-content [name=name]').value, 'Original project');
  submit('Edited project'); await flush();
  assert.equal(ui.projectCrumb.textContent, 'Edited project'); assert.match(ui.projects.textContent, /Edited project/);
  editName('channel'); submit('Edited channel'); await flush();
  assert.equal(ui.channelCrumb.textContent, 'Edited channel'); assert.equal(ui.channelTitle.textContent, 'Edited channel'); assert.match(ui.channels.textContent, /Edited channel/);
  assert.equal(state.thread.id, 'thread'); assert.equal(state.messages[0].text, 'Keep message'); assert.equal(state.messageCursor, 7); assert.equal(ui.input.value, 'Keep draft'); assert.equal(state.replyTo.id, 'message');
  assert.deepEqual(requests.filter(r => r.method === 'PATCH').map(r => r.body), [{ name: 'Edited project' }, { name: 'Edited channel' }]);
  project = { ...project, name: '<img src=x onerror=alert(1)>' }; channel = { ...channel, name: 'Renamed elsewhere' }; events = [{ id: 1, type: 'project.updated' }, { id: 2, type: 'channel.updated' }];
  await pollEvents('project', state.projectGeneration);
  assert.equal(ui.projectCrumb.textContent, project.name); assert.equal(ui.projectCrumb.querySelector('img'), null);
  assert.equal(ui.channelTitle.textContent, 'Renamed elsewhere'); assert.equal(ui.input.value, 'Keep draft'); assert.equal(state.thread.id, 'thread');
  assert.equal(requests.filter(r => r.url.includes('/messages')).length, 0, 'renaming does not reload or clear conversation');
  state.members = [{ actor: me, role: 'guest' }]; updateNameControls(); assert.equal(w.document.querySelector('#edit-project').hidden, true); assert.equal(w.document.querySelector('#edit-channel').hidden, true);
  state.members = [{ actor: me, role: 'owner' }]; delayed = {}; editName('project'); submit('Late rename'); await flush();
  state.project = { id: 'other', name: 'Other project' }; state.projectGeneration++; ui.projectCrumb.textContent = 'Other project';
  delayed.resolve(); await flush(); assert.equal(ui.projectCrumb.textContent, 'Other project', 'late edit cannot replace newer project');
  dom.window.close(); process.stdout.write('Name-editing DOM checks passed: owner permissions, live labels, preserved messages/drafts, safe rendering and late navigation.\n');
})().catch(error => { console.error(error); process.exit(1); });
