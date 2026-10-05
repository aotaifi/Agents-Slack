#!/usr/bin/env node
'use strict';
// DOM integration checks with a server-shaped transport and delayed responses.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { JSDOM } = process.env.JSDOM_PATH ? createRequire(path.resolve(process.env.JSDOM_PATH, 'package.json'))('jsdom') : require('jsdom');
const staticDir = path.resolve(__dirname, '../src/agent_commons/static');
const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');
const source = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
const human = { id: 'human', name: 'Researcher', handle: 'researcher', kind: 'human', has_password: true };
const actor = { id: 'agent', name: '<img src=x> Scout', kind: 'agent', handle: 'researcher.scout', owner: human };
const projects = [{ id: 'a', name: 'Study A' }, { id: 'b', name: 'Study B' }];
const channel = id => ({ id: `channel-${id}`, project_id: id, name: 'Field notes' });
const thread = id => ({ id: `thread-${id}`, channel_id: `channel-${id}`, project_id: id, title: 'Evidence' });
const root = { id: 'root', text: 'Finding', sequence: 1, author: actor, reply_to: null, reactions: [], reply_count: 1 };
const reply = { id: 'reply', text: 'Mention in an older reply', sequence: 101, author: actor, reply_to: 'root', reactions: [] };
const mention = (id, project = 'b', message = reply) => ({ id, created_at: '2026-10-02T12:00:00Z', read_at: null, project: projects.find(p => p.id === project), channel: channel(project), thread: thread(project), message: { id: message.id, reply_to: message.reply_to, author: actor, text: '<script>unsafe()</script> mention preview', truncated: true } });
const response = (data, status = 200) => ({ status, ok: status < 400, json: async () => structuredClone(data) });
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
async function flush() { for (let i = 0; i < 8; i++) await tick(); }
async function create(items = [mention(3), mention(2, 'a', root)], me = human) {
  const dom = new JSDOM(html, { url: 'https://workspace.test', runScripts: 'outside-only', pretendToBeVisual: true }); const w = dom.window;
  w.Headers = Headers; w.crypto.randomUUID = require('node:crypto').randomUUID;
  const timers = new Map(); let nextTimer = 0;
  w.setInterval = (callback, ms) => { const id = ++nextTimer; timers.set(id, { callback, ms }); return id; }; w.clearInterval = id => timers.delete(id);
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; }; w.HTMLDialogElement.prototype.close = function () { this.open = false; this.dispatchEvent(new w.Event('close')); };
  const requests = []; let hook = null; let store = structuredClone(items);
  const route = async request => {
    const { url, method, body } = request;
    if (url === '/v1/me') return response(me);
    if (url === '/v1/projects') return response({ items: projects });
    if (url === '/v1/actors') return response({ items: [human, actor] });
    if (url.includes('/events')) return response({ items: [], cursor: 0, next_cursor: null });
    if (url.endsWith('/channels')) return response({ items: [channel(url.includes('/a/') ? 'a' : 'b')] });
    if (url.endsWith('/members')) return response({ items: [{ actor: human, role: 'owner' }] });
    if (url.endsWith('/threads')) return response({ items: [thread(url.includes('channel-a') ? 'a' : 'b')] });
    if (url.includes('/messages?')) return response({ items: url.includes('after=0&') ? [root] : [reply], next_cursor: url.includes('after=0&') ? 100 : null, cursor: 101 });
    if (url.startsWith('/v1/notifications?')) {
      const before = Number(new URL(url, w.location.href).searchParams.get('before')); const all = store.slice().sort((a, b) => b.id - a.id); const found = before ? all.filter(i => i.id < before) : all; const page = found.slice(0, 50);
      return response({ items: page, next_cursor: found.length > 50 ? page.at(-1).id : null, cursor: all[0]?.id || null, unread_count: all.filter(i => !i.read_at).length });
    }
    if (url === '/v1/notifications/read-all') { store.forEach(i => { if (i.id <= body.through) i.read_at = '2026-10-02T13:00:00Z'; }); return response({ updated: store.length }); }
    if (url.startsWith('/v1/notifications/') && method === 'PATCH') { const item = store.find(i => i.id === Number(url.split('/').at(-1))); item.read_at = body.read ? '2026-10-02T13:00:00Z' : null; return response(item); }
    if (url === '/v1/auth/logout') return response(null, 204);
    throw new Error(`Unexpected request: ${url}`);
  };
  w.fetch = async (url, options = {}) => { const req = { url, method: options.method || 'GET', body: options.body ? JSON.parse(options.body) : null }; requests.push(req); return (hook && await hook(req)) || route(req); };
  w.eval(source.replace('  const savedToken = state.token;', '  window.__inboxTest = { state, ui, notifications, showInbox, pollNotifications, setNotificationRead, markAllNotificationsRead, openNotification, selectProject, selectChannel, signOut, enterWorkspace, passwordSignIn };\n  const savedToken = state.token;'));
  await flush(); return { w, requests, timers, route, ...w.__inboxTest, setHook(fn) { hook = fn; }, setStore(value) { store = structuredClone(value); }, getStore() { return structuredClone(store); } };
}
const watchdog = setTimeout(() => { console.error('Inbox checks timed out with an unresolved operation.'); process.exit(1); }, 10000);
(async () => {
  const app = await create(); const d = app.w.document;
  assert.equal(app.ui.inbox.hidden, false); assert.equal(app.ui.inboxBadge.textContent, '2'); assert.equal(app.ui.mentionToast.hidden, true, 'first baseline stays quiet');
  assert.equal([...app.timers.values()].filter(t => t.ms === 5000).length, 1, 'one global five-second poll');
  const writes = () => app.requests.filter(r => r.method === 'PATCH' && r.url.startsWith('/v1/notifications/'));
  app.ui.input.value = 'Preserve this draft'; app.state.replyTo = root; app.showInbox(); await flush();
  assert.equal(app.ui.input.value, 'Preserve this draft'); assert.equal(app.state.replyTo.id, root.id); assert.equal(writes().length, 0, 'listing never reads');
  d.querySelector('[data-notification-id="3"] [data-notification-action="open"]').focus(); await app.pollNotifications(); assert.equal(d.activeElement.dataset.notificationAction, 'open'); assert.equal(d.activeElement.closest('[data-notification-id]').dataset.notificationId, '3', 'polling preserves keyboard focus');
  assert.equal(d.querySelector('#human-inbox script, #human-inbox img'), null, 'previews and author names are plain text');
  await app.setNotificationRead(app.notifications.items[0], true); assert.equal(app.ui.inboxBadge.textContent, '1');
  await app.setNotificationRead(app.notifications.items[0], false); assert.equal(app.ui.inboxBadge.textContent, '2');
  app.setHook(req => req.method === 'PATCH' ? response({ detail: 'offline' }, 503) : null);
  await app.setNotificationRead(app.notifications.items[0], true); assert.equal(app.ui.inboxBadge.textContent, '2'); assert.equal(app.notifications.items[0].read_at, null);
  app.setHook(req => req.url.endsWith('channel-b/threads') ? response({ detail: 'gone' }, 404) : null);
  const beforeFailed = writes().length; assert.equal(await app.openNotification(app.notifications.items[0]), false); assert.equal(writes().length, beforeFailed, 'failed conversation does not read');
  app.setHook(null); const target = app.notifications.items[0]; assert.equal(await app.openNotification(target), true);
  assert.equal(app.state.project.id, 'b'); assert.equal(app.state.thread.id, 'thread-b'); assert.equal(d.activeElement.dataset.messageId, 'reply'); assert.equal(app.ui.messages.querySelector('.message-replies').hidden, false);
  assert(app.requests.some(r => r.url.includes('/thread-b/messages?after=100&')), 'older message pages were loaded'); assert.equal(writes().at(-1).body.read, true); assert.equal(app.ui.inboxBadge.textContent, '1');
  app.showInbox(); await flush(); app.setHook(req => { if (req.url === '/v1/notifications/read-all') { app.setStore([...app.getStore(), mention(4)]); assert.equal(req.body.through, 3, 'read-all uses displayed snapshot'); } return null; });
  await app.markAllNotificationsRead(); assert.equal(app.ui.inboxBadge.textContent, '1', 'mention arriving after displayed snapshot stays unread'); assert.equal(app.getStore().find(i => i.id === 4).read_at, null);
  app.setHook(null);
  Object.defineProperty(d, 'visibilityState', { configurable: true, value: 'hidden' }); app.ui.mentionToast.hidden = true; app.setStore([...app.getStore(), mention(5)]); await app.pollNotifications(); assert.equal(app.ui.mentionToast.hidden, true, 'hidden app stays quiet');
  Object.defineProperty(d, 'visibilityState', { configurable: true, value: 'visible' }); app.setStore([...app.getStore(), mention(6), mention(7)]); await app.pollNotifications(); assert.match(app.ui.mentionToast.textContent, /2 new mentions/);
  let release; let listCalls = 0;
  app.setHook(req => req.url.startsWith('/v1/notifications?') ? (listCalls++, new Promise(resolve => { release = () => resolve(response({ items: [], next_cursor: null, cursor: 999, unread_count: 999 })); })) : null);
  const pending = app.pollNotifications(); await tick(); await app.pollNotifications(); assert.equal(listCalls, 1, 'polling has one request in flight');
  await app.signOut(); release(); await pending; assert.equal(app.state.me, null); assert.equal(app.ui.inbox.hidden, true); assert.equal(app.ui.inboxBadge.textContent, ''); assert.equal(app.notifications.items.length, 0); assert.equal(app.timers.size, 0); assert.equal(d.querySelector('#human-inbox'), null, 'sign-out removes private inbox content'); app.w.close();

  const paging = await create(Array.from({ length: 51 }, (_, i) => mention(i + 1)));
  paging.showInbox(); await flush(); assert.equal(paging.notifications.items.length, 50); assert.equal(paging.notifications.next, 2); assert.equal(paging.w.document.querySelector('#human-inbox button:last-child').disabled, false);
  await paging.pollNotifications(true); assert.equal(paging.notifications.items.length, 51); assert.equal(paging.notifications.next, null); assert.equal(paging.notifications.displayedCursor, 51); assert.equal(paging.requests.filter(r => r.method === 'PATCH').length, 0); paging.w.close();

  for (const change of ['navigation', 'signout', 'account']) {
    const race = await create(); race.showInbox(); await flush(); let finish; const target = race.notifications.items[0];
    race.setHook(req => req.url.includes('/thread-b/messages?') ? new Promise(resolve => { finish = () => resolve(response({ items: [root, reply], next_cursor: null, cursor: 101 })); }) : null);
    const opening = race.openNotification(target); await flush(); assert.equal(typeof finish, 'function');
    if (change === 'navigation') await race.selectChannel(channel('a'));
    else if (change === 'signout') await race.signOut();
    else { race.state.authGeneration++; await race.enterWorkspace({ ...human, id: 'other-human' }, race.state.authGeneration); }
    finish(); await opening; assert.equal(race.requests.some(r => r.method === 'PATCH' && r.url === '/v1/notifications/3'), false, `${change} prevents delayed navigation reading`);
    if (change !== 'navigation') assert.equal(race.state.messages.some(m => m.id === reply.id), false, `${change} ignores old message response`); race.w.close();
  }
  const readRace = await create(); readRace.showInbox(); await flush(); let staleList;
  readRace.setHook(req => req.url.startsWith('/v1/notifications?') ? new Promise(resolve => { staleList = () => resolve(response({ items: [mention(3), mention(2, 'a', root)], cursor: 3, next_cursor: null, unread_count: 2 })); }) : null);
  const stalePoll = readRace.pollNotifications(); await tick(); await readRace.setNotificationRead(readRace.notifications.items[0], true);
  readRace.setHook(null); staleList(); await stalePoll; await flush(); assert.equal(readRace.ui.inboxBadge.textContent, '1', 'pre-write poll cannot overwrite persisted read count'); assert(readRace.notifications.items[0].read_at); readRace.w.close();

  const bulkRace = await create(); bulkRace.showInbox(); await flush(); let finishBulk;
  bulkRace.setHook(req => req.url === '/v1/notifications/read-all' ? new Promise(resolve => { finishBulk = async () => resolve(await bulkRace.route(req)); }) : null);
  const bulk = bulkRace.markAllNotificationsRead(); await tick(); const writesBefore = bulkRace.requests.length;
  assert.equal(await bulkRace.setNotificationRead(bulkRace.notifications.items[0], false), false, 'bulk write blocks conflicting row mutation');
  assert.equal(await bulkRace.openNotification(bulkRace.notifications.items[0]), false); assert.equal(bulkRace.requests.length, writesBefore);
  assert([...bulkRace.w.document.querySelectorAll('.inbox-actions button')].every(button => button.disabled)); await finishBulk(); await bulk; assert.equal(bulkRace.notifications.unread, 0); bulkRace.w.close();

  const authPoll = await create(); let finishOld; let firstDelayed = true;
  authPoll.setHook(req => req.url.startsWith('/v1/notifications?') && firstDelayed ? (firstDelayed = false, new Promise(resolve => { finishOld = () => resolve(response({ items: [mention(999)], cursor: 999, next_cursor: null, unread_count: 999 })); })) : null);
  const oldAuthPoll = authPoll.pollNotifications(); await tick(); authPoll.state.authGeneration++; authPoll.setStore([]);
  await authPoll.enterWorkspace({ ...human, id: 'other-human' }, authPoll.state.authGeneration); await flush(); assert.equal(authPoll.notifications.unread, 0, 'new identity polls while old account response is pending');
  finishOld(); await oldAuthPoll; assert.equal(authPoll.notifications.unread, 0); assert.equal(authPoll.notifications.items.length, 0, 'old poll cannot leak into new account'); authPoll.w.close();

  for (const rejectProjects of [false, true]) {
    const privateApp = await create(); let finishProjects;
    privateApp.state.projects = [{ id: 'a', name: 'Private A project' }]; privateApp.state.project = privateApp.state.projects[0]; privateApp.state.channel = { ...channel('a'), name: 'Private A channel' }; privateApp.state.thread = { ...thread('a'), title: 'Private A conversation' }; privateApp.state.messages = [root];
    for (const element of [privateApp.ui.projects, privateApp.ui.channels, privateApp.ui.projectCrumb, privateApp.ui.channelCrumb, privateApp.ui.projectDescription, privateApp.ui.channelTitle, privateApp.ui.channelDescription, privateApp.ui.threadTitle]) element.textContent = 'Private A information';
    privateApp.ui.messages.textContent = 'Private A message'; privateApp.ui.input.value = 'Private A draft';
    privateApp.setHook(req => req.url === '/v1/projects' ? new Promise(resolve => { finishProjects = () => resolve(rejectProjects ? response({ detail: 'Unavailable' }, 503) : response({ items: [] })); }) : null);
    privateApp.state.authGeneration++; const changingAccount = privateApp.enterWorkspace({ ...human, id: 'other-human' }, privateApp.state.authGeneration);
    const assertPrivateCleared = () => {
      assert.equal(privateApp.state.projects.length, 0); assert.equal(privateApp.state.project, null); assert.equal(privateApp.ui.input.value, '');
      for (const element of [privateApp.ui.projects, privateApp.ui.channels, privateApp.ui.projectCrumb, privateApp.ui.channelCrumb, privateApp.ui.projectDescription, privateApp.ui.channelTitle, privateApp.ui.channelDescription, privateApp.ui.threadTitle, privateApp.ui.messages]) assert.doesNotMatch(element.textContent, /Private A/, 'previous account names disappear before project lookup completes');
    };
    assertPrivateCleared(); await tick(); assert.equal(typeof finishProjects, 'function'); finishProjects();
    if (rejectProjects) await assert.rejects(changingAccount, /Unavailable/); else await changingAccount;
    assertPrivateCleared(); privateApp.w.close();
  }

  for (const rejectProjects of [false, true]) {
    const signedOutApp = await create(); let finishProjects;
    const privateElements = [signedOutApp.ui.projects, signedOutApp.ui.channels, signedOutApp.ui.projectCrumb, signedOutApp.ui.channelCrumb, signedOutApp.ui.projectDescription, signedOutApp.ui.channelTitle, signedOutApp.ui.channelDescription, signedOutApp.ui.threadTitle, signedOutApp.ui.modalContent, signedOutApp.ui.modalTitle];
    for (const element of privateElements) element.textContent = 'Private A after sign-out';
    await signedOutApp.signOut(); assert.equal(signedOutApp.state.me, null);
    signedOutApp.setHook(req => req.url === '/v1/auth/login' ? response({ actor: { ...human, id: 'other-human' } }) : req.url === '/v1/projects' ? new Promise(resolve => { finishProjects = () => resolve(rejectProjects ? response({ detail: 'Unavailable' }, 503) : response({ items: [] })); }) : null);
    const login = signedOutApp.passwordSignIn('other-human', 'password used by fixture'); await flush(); assert.equal(typeof finishProjects, 'function'); assert.equal(signedOutApp.ui.workspace.hidden, false);
    for (const element of privateElements) assert.doesNotMatch(element.textContent, /Private A/, 'sign-out clears names before another account loads projects');
    finishProjects(); await login;
    for (const element of privateElements) assert.doesNotMatch(element.textContent, /Private A/, 'sign-out names stay cleared after project success or failure'); signedOutApp.w.close();
  }

  const agentApp = await create([], { ...actor, has_password: false }); assert.equal(agentApp.ui.inbox.hidden, true); assert.equal(agentApp.requests.some(r => r.url.startsWith('/v1/notifications')), false); agentApp.w.close();
  console.log('Human inbox DOM checks passed: quiet baseline, global badge, explicit read/unread, safe previews, drafts, failed navigation, cross-project older reply focus, snapshot read-all, visibility, one in-flight poll, read/poll and bulk-write races, pagination, late auth/navigation responses and human-only access.');
})().catch(error => { console.error(error); process.exitCode = 1; }).finally(() => clearTimeout(watchdog));
