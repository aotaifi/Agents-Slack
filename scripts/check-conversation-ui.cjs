#!/usr/bin/env node
// DOM-only regression checks. Install jsdom separately and set JSDOM_PATH to its
// package directory, or install it in node's normal module lookup path.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { JSDOM } = process.env.JSDOM_PATH
  ? createRequire(path.resolve(process.env.JSDOM_PATH, 'package.json'))('jsdom')
  : require('jsdom');
const staticDir = path.resolve(__dirname, '../src/agent_commons/static');
const dom = new JSDOM(fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8'), {
  url: 'https://workspace.test', runScripts: 'outside-only', pretendToBeVisual: true,
});
const { window } = dom;
window.Headers = Headers;
window.crypto.randomUUID = require('node:crypto').randomUUID;
const source = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
window.eval(source.replace("  const savedToken = state.token;", "  window.__uiTest = { state, ui, renderMessages, pollEvents, sendMessage, loadMessages };\n  const savedToken = state.token;"));
const { state, ui, renderMessages, pollEvents, sendMessage } = window.__uiTest;
const me = { id: 'human-1', name: 'Researcher', handle: 'researcher', kind: 'human', owner: null };
const agent = { id: 'agent-1', name: 'Scout', handle: 'researcher.scout', kind: 'agent', owner: me };
const root = { id: 'root', sequence: 1, text: 'Findings 👍', author: agent, created_at: '2026-09-30T10:00:00Z', reply_to: null, reply_count: 1, reactions: [] };
const reply = { id: 'reply', sequence: 2, text: 'One supporting reply', author: me, reply_to: 'root', reply_count: 0, reactions: [] };
let snapshot = [root, reply];
const requests = [];
let events = [];
state.me = me; state.token = 'test-token'; state.project = { id: 'project' }; state.thread = { id: 'thread' };
state.messages = structuredClone(snapshot);
window.fetch = async (url, options = {}) => {
  const body = options.body ? JSON.parse(options.body) : null;
  requests.push({ url, method: options.method || 'GET', body });
  let result;
  if (url.includes('/reactions')) {
    const message = snapshot.find(m => url.includes(`/messages/${m.id}/`));
    message.reactions = options.method === 'DELETE' ? [] : [{ emoji: body.emoji, count: 1, actors: [me] }];
    result = message;
  } else if (url.includes('/events')) result = { items: events, cursor: 3, next_cursor: null };
  else if (options.method === 'POST') result = { id: 'new-reply' };
  else result = { items: snapshot, cursor: 2, next_cursor: null };
  return { status: 200, ok: true, json: async () => structuredClone(result) };
};
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const firstRoot = () => ui.messages.querySelector('.message-group');
const reaction = () => firstRoot().querySelector('.message-reactions button');
(async () => {
  assert.equal(window.document.title, 'Research Workspace · Research messaging');
  renderMessages(true);
  assert.match(firstRoot().querySelector('.message-author').textContent, /@researcher\.scout/);
  const chip = firstRoot().querySelector('.badge');
  assert.equal(chip.firstChild.textContent, 'Agent');
  assert.match(chip.title, /owned by Researcher \(@researcher\)/);
  assert.match(chip.querySelector('.visually-hidden').textContent, /owned by Researcher \(@researcher\)/);
  assert.equal(firstRoot().querySelector('.replies-toggle').textContent, 'Show 1 reply');
  assert.equal(firstRoot().querySelector('.message-replies').hidden, true);
  firstRoot().querySelector('.replies-toggle').click();
  assert.equal(firstRoot().querySelector('.message-replies').hidden, false);
  assert.equal(firstRoot().querySelectorAll('.message-replies .message').length, 1);
  firstRoot().querySelector('.message-replies .reply-button').click();
  assert.equal(state.replyTo.id, 'root', 'replying to a reply targets its root');
  ui.input.value = 'Another reply';
  await sendMessage({ preventDefault() {} });
  const post = requests.find(r => r.method === 'POST');
  assert.equal(post.body.reply_to, 'root');
  assert.equal(post.body.text, 'Another reply');
  assert.equal(post.body.metadata, undefined, 'explicit reply_to contract');
  assert.equal(firstRoot().querySelector('.message-replies').hidden, false);
  assert.equal(reaction().getAttribute('aria-pressed'), 'false');
  reaction().click(); await tick();
  const added = requests.find(r => r.method === 'PUT');
  assert.equal(added.url, '/v1/messages/root/reactions');
  assert.deepEqual(added.body, { emoji: '👍' });
  assert.equal(reaction().getAttribute('aria-pressed'), 'true');
  assert.equal(reaction().textContent, '👍 1');
  assert.match(reaction().title, /Researcher \(@researcher\)/);
  reaction().click(); await tick();
  const removed = requests.find(r => r.method === 'DELETE');
  assert.deepEqual(removed.body, { emoji: '👍' });
  assert.equal(reaction().getAttribute('aria-pressed'), 'false');
  assert.equal(reaction().textContent, '👍');
  const malicious = '<img src=x onerror="window.__xss=true">' + 'evidence '.repeat(150);
  snapshot[0].text = malicious;
  state.messages = structuredClone(snapshot); renderMessages();
  assert.equal(firstRoot().querySelector('.message-text').textContent, malicious.slice(0, 800) + '…');
  assert.equal(firstRoot().querySelectorAll('img, script').length, 0);
  firstRoot().querySelector('.detail-toggle').click();
  assert.equal(firstRoot().querySelector('.message-text').textContent, malicious);
  assert.equal(window.__xss, undefined);
  ui.input.value = 'Keep this draft'; state.replyTo = state.messages[0];
  snapshot[0].reactions = [{ emoji: '👀', count: 1, actors: [agent] }];
  snapshot[0].reply_count = 2;
  snapshot.push({ ...reply, id: 'reply-2', sequence: 3, text: 'New reply' });
  events = [{ id: 1, type: 'message.created', thread_id: 'thread' }, { id: 2, type: 'reaction.added', thread_id: 'thread' }];
  const beforeRefresh = requests.filter(r => r.url.includes('/threads/thread/messages')).length;
  await pollEvents('project', state.projectGeneration);
  assert.equal(requests.filter(r => r.url.includes('/threads/thread/messages')).length - beforeRefresh, 1, 'batch events share one snapshot refresh');
  assert.equal(firstRoot().querySelector('.replies-toggle').textContent, 'Hide 2 replies');
  assert.equal(firstRoot().querySelector('.message-text').textContent, malicious, 'expanded long message survives polling');
  assert.equal(firstRoot().querySelector('.message-replies').hidden, false);
  assert.equal(firstRoot().querySelector('.message-reactions .reaction-count').textContent, '👀 1');
  assert.deepEqual([...firstRoot().querySelector('.message-reactions').querySelectorAll('button')].map(b => b.dataset.messageAction), ['reaction-👍', 'reaction-❓']);
  assert.equal(ui.input.value, 'Keep this draft');
  assert.equal(state.replyTo.id, 'root');
  assert.equal(state.thread.id, 'thread');
  assert.equal(window.__xss, undefined);
  // Canonical null wins over invalid legacy metadata rejected during migration.
  state.messages = [...structuredClone(snapshot), { ...reply, id: 'legacy-invalid', sequence: 4, reply_to: null, metadata: { reply_to: 'reply' } }];
  renderMessages();
  assert.equal(ui.messages.querySelectorAll('.message-group').length, 2);
  ui.messages.querySelector('[data-message-id="legacy-invalid"] .reply-button').click();
  assert.equal(state.replyTo.id, 'legacy-invalid');
  // Events received after a same-project conversation switch refresh the current view.
  state.eventCursor = 10; state.pollBusy = false; state.thread = { id: 'thread-a' };
  let releaseEvents;
  let currentRefreshes = 0;
  window.fetch = async url => {
    if (url.includes('/events')) return new Promise(resolve => { releaseEvents = () => resolve({ status: 200, ok: true, json: async () => ({ items: [{ id: 11, type: 'message.created', thread_id: 'thread-b' }], cursor: 11, next_cursor: null }) }); });
    assert.match(url, /threads\/thread-b\/messages/);
    currentRefreshes++;
    return { status: 200, ok: true, json: async () => ({ items: [{ ...root, id: 'b-message', thread_id: 'thread-b', sequence: 11 }], cursor: 11, next_cursor: null }) };
  };
  const switchingPoll = pollEvents('project', state.projectGeneration);
  state.navigationGeneration++; state.thread = { id: 'thread-b' }; state.messages = []; state.messageCursor = 0;
  releaseEvents(); await switchingPoll;
  assert.equal(currentRefreshes, 1);
  assert.equal(state.messages[0].id, 'b-message');
  assert.equal(state.eventCursor, 11);
  // A delayed refresh for project A must not advance project B's event cursor.
  state.eventCursor = 0; state.pollBusy = false; state.thread = { id: 'thread' };
  let releaseRefresh;
  let refreshStarted;
  const refreshReady = new Promise(resolve => { refreshStarted = resolve; });
  window.fetch = async url => {
    if (url.includes('/events')) return { status: 200, ok: true, json: async () => ({ items: [{ id: 100, type: 'message.created', thread_id: 'thread' }], cursor: 100, next_cursor: null }) };
    refreshStarted();
    return new Promise(resolve => { releaseRefresh = () => resolve({ status: 200, ok: true, json: async () => ({ items: snapshot, cursor: 100, next_cursor: null }) }); });
  };
  const oldPoll = pollEvents('project', state.projectGeneration);
  await refreshReady;
  state.projectGeneration++; state.navigationGeneration++;
  state.project = { id: 'project-b' }; state.thread = { id: 'thread-b' }; state.eventCursor = 3;
  releaseRefresh(); await oldPoll;
  assert.equal(state.eventCursor, 3, 'old project refresh cannot skip new project events');
  console.log('Conversation DOM checks passed: handles/ownership, one reply, root targeting, reaction add/remove, safe long text, event refresh and draft preservation.');
  window.close();
})().catch(error => { console.error(error); window.close(); process.exitCode = 1; });
