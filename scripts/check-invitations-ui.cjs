#!/usr/bin/env node
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { JSDOM } = process.env.JSDOM_PATH
  ? createRequire(path.resolve(process.env.JSDOM_PATH, 'package.json'))('jsdom') : require('jsdom');
const directory = path.resolve(__dirname, '../src/agent_commons/static');
const source = fs.readFileSync(path.join(directory, 'app.js'), 'utf8');
const html = fs.readFileSync(path.join(directory, 'index.html'), 'utf8');
const owner = { id: 'owner', name: 'Owner', handle: 'owner', kind: 'human', is_admin: true, has_password: true };
const guest = { id: 'guest', name: 'Alex', handle: 'alex', kind: 'human', is_admin: false, has_password: true };
const project = { id: 'project', name: '<img src=x onerror=alert(1)>', description: '' };
const code = 'c'.repeat(43);
const invitation = { id: 'invite', code, role: 'guest', used: false, revoked: false, expires_at: '2099-01-01T00:00:00Z' };
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
async function flush() { for (let i = 0; i < 10; i++) await tick(); }
function setup(url = 'http://127.0.0.1:8000/', token = '', emailEnabled = true) {
  const dom = new JSDOM(html, { url, runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window; w.Headers = Headers; w.crypto.randomUUID = require('node:crypto').randomUUID;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  w.setInterval = () => 1; w.clearInterval = () => {};
  let signedIn = token ? owner : null; const requests = []; let members = [{ actor: owner, role: 'owner' }];
  if (token) w.sessionStorage.setItem('commons_token', token);
  w.fetch = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : undefined;
    requests.push({ url, method: options.method || 'GET', body, headers: options.headers });
    let data;
    if (url.startsWith('/v1/notifications?')) data = { items: [], next_cursor: null, cursor: null, unread_count: 0 };
    else if (url === '/v1/connection') data = { ssh_host: 'lab.example.test', ssh_app_port: 18000, local_port: 8002, email_enabled: emailEnabled };
    else if (url === '/v1/invitations/preview') data = { project, role: 'guest', expires_at: invitation.expires_at };
    else if (url === '/v1/invitations/accept') { signedIn = guest; members.push({ actor: guest, role: 'guest' }); data = { actor: guest, token: token ? null : 'guest-token', project, role: 'guest' }; }
    else if (url === '/v1/auth/login') { signedIn = guest; data = { actor: guest }; }
    else if (url === '/v1/auth/logout') { signedIn = null; return { status: 204, ok: true }; }
    else if (url === '/v1/me') { if (!signedIn) return { status: 401, ok: false, json: async () => ({}) }; data = signedIn; }
    else if (url === '/v1/projects') data = { items: [project] };
    else if (url === '/v1/actors') data = { items: [owner, guest] };
    else if (url.endsWith('/role')) { members.find(m => url.includes(`/members/${m.actor.id}/`)).role = body.role; data = {}; }
    else if (url.endsWith('/members')) data = { items: members };
    else if (url.endsWith('/invitations')) data = options.method === 'POST' ? invitation : { items: [invitation] };
    else data = { items: [], cursor: 0, next_cursor: null };
    return { status: 200, ok: true, json: async () => structuredClone(data) };
  };
  w.eval(source.replace('  const savedToken = state.token;', '  window.__invitationTest = { state, showMembers, createInvitation, openInvitation };\n  const savedToken = state.token;'));
  return { dom, w, requests, setMembers: values => { members = values; } };
}
(async () => {
  const admin = setup(); await flush(); const { w, requests } = admin; const api = w.__invitationTest;
  api.state.me = owner; api.state.token = 'owner-token'; api.state.project = project;
  await api.showMembers();
  const inviteButton = [...w.document.querySelectorAll('button')].find(b => b.textContent === 'Invite researcher');
  assert.ok(inviteButton);
  inviteButton.click(); await flush();
  const role = w.document.querySelector('[aria-label="Invitation role"]'); assert.equal(role.value, 'guest');
  const recipient = w.document.querySelector('[name=email]'); assert.equal(recipient.required, false); recipient.value = 'alex+lab@example.test';
  role.value = 'owner';
  w.document.querySelector('#modal-content form').dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true })); await flush();
  const created = requests.find(r => r.method === 'POST' && r.url.endsWith('/invitations'));
  assert.deepEqual(created.body, { role: 'owner', expires_in_hours: 72 });
  const instructions = w.document.querySelector('[aria-label="Invitation and connection instructions"]').value;
  assert.match(instructions, /YOUR_UNIVERSITY_USERNAME@lab\.example\.test/);
  assert.match(instructions, /127\.0\.0\.1:8002:127\.0\.0\.1:18000/);
  assert.match(instructions, new RegExp(`http://127\\.0\\.0\\.1:8002/#invite=${code}`));
  assert.match(instructions, /Accept invitation/); assert.match(instructions, /password/); assert.match(instructions, /Create an agent/);
  const draft = w.document.querySelector('a[href^="mailto:"]'); const draftUrl = new URL(draft.href);
  assert.equal(decodeURIComponent(draftUrl.pathname), 'alex+lab@example.test');
  assert.equal(draftUrl.searchParams.get('body').replace(/\r\n/g, '\n'), instructions);
  assert.ok(draftUrl.searchParams.get('subject').includes(project.name));
  assert.equal(w.document.querySelector('[name=recipient]').value, 'alex+lab@example.test');
  const mailForm = w.document.querySelector('#modal-content form');
  const normalFetch = w.fetch; let failEmail = true;
  w.fetch = (url, options) => url.endsWith('/email') && failEmail
    ? Promise.resolve({ status: 502, ok: false, json: async () => ({ detail: 'Email submission could not be confirmed.' }) }) : normalFetch(url, options);
  mailForm.dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true })); await flush();
  assert.match(mailForm.querySelector('[role=status]').textContent, /could not be confirmed/);
  assert.equal(mailForm.querySelector('button').disabled, false);
  assert.equal(w.document.querySelector('[aria-label="Invitation and connection instructions"]').value, instructions);
  w.fetch = (url, options) => url.endsWith('/email') ? Promise.reject(new Error('Connection lost after SMTP acceptance')) : normalFetch(url, options);
  mailForm.dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true })); await flush();
  assert.match(mailForm.querySelector('[role=status]').textContent, /may have been sent/);
  assert.match(mailForm.querySelector('[role=status]').textContent, /Check before retrying/);
  assert.equal(w.document.querySelector('[aria-label="Invitation and connection instructions"]').value, instructions);
  w.fetch = normalFetch;
  failEmail = false;
  mailForm.dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true })); await flush();
  const emailed = requests.find(r => r.url.endsWith('/email'));
  assert.deepEqual(emailed.body, { code, to: 'alex+lab@example.test' });
  assert.match(mailForm.querySelector('[role=status]').textContent, /Inbox delivery is not yet confirmed/);
  assert.equal(mailForm.querySelector('button').disabled, true);
  assert.ok(!requests.some(r => r.url.includes(code)), 'secret never placed in API URL');
  await api.showMembers();
  const picker = w.document.querySelector('[aria-label="Role for Owner"]'); picker.value = 'guest';
  [...w.document.querySelectorAll('button')].find(b => b.textContent === 'Save role').click(); await flush();
  assert.deepEqual(requests.find(r => r.url.endsWith('/role')).body, { role: 'guest' });
  api.state.me = guest; admin.setMembers([{ actor: guest, role: 'guest' }]); await api.showMembers();
  assert.ok(![...w.document.querySelectorAll('button')].some(b => b.textContent === 'Invite researcher'));
  assert.equal(w.document.querySelectorAll('[aria-label^="Role for"]').length, 0);
  admin.dom.window.close();

  const disabledMail = setup('https://workspace.example.test/', '', false);
  await flush(); disabledMail.w.__invitationTest.state.me = owner; disabledMail.w.__invitationTest.state.token = 'owner-token'; disabledMail.w.__invitationTest.state.project = project;
  await disabledMail.w.__invitationTest.createInvitation(project.id);
  disabledMail.w.document.querySelector('#modal-content form').dispatchEvent(new disabledMail.w.Event('submit', { bubbles: true, cancelable: true })); await flush();
  assert.equal(disabledMail.w.document.querySelector('[name=recipient]'), null);
  const publicInstructions = disabledMail.w.document.querySelector('[aria-label="Invitation and connection instructions"]').value;
  assert.ok(publicInstructions.includes(`https://workspace.example.test/#invite=${code}`));
  assert.ok(!publicInstructions.includes('ssh -N'));
  assert.equal(disabledMail.w.document.querySelector('a[href^="mailto:"]').getAttribute('href').startsWith('mailto:?subject='), true);
  assert.ok(disabledMail.w.document.querySelector('#modal-content').textContent.includes('Server email is not configured'));
  disabledMail.dom.window.close();

  const joining = setup(`http://127.0.0.1:8002/#invite=${code}`); await flush();
  assert.equal(joining.w.location.hash, '', 'remove invitation from address bar/history');
  assert.equal(joining.w.document.querySelector('#invitation-view').hidden, false);
  assert.equal(joining.w.document.querySelector('#invitation-content img'), null, 'project name is text');
  joining.w.document.querySelector('#invitation-content [name=name]').value = 'Alex';
  joining.w.document.querySelector('#invitation-content [name=handle]').value = 'alex';
  joining.w.document.querySelector('#invitation-content [name=password]').value = 'chosen invitation password';
  if (joining.w.document.querySelector('#invitation-content [name=confirm_password]')) joining.w.document.querySelector('#invitation-content [name=confirm_password]').value = 'chosen invitation password';
  joining.w.document.querySelector('#invitation-content form').dispatchEvent(new joining.w.Event('submit', { bubbles: true, cancelable: true })); await flush();
  const accepted = joining.requests.find(r => r.url === '/v1/invitations/accept');
  assert.deepEqual({ ...accepted.body, claim_secret: undefined }, { code, name: 'Alex', handle: 'alex', password: 'chosen invitation password', claim_secret: undefined });
  assert.match(accepted.body.claim_secret, /^[0-9a-f]{64}$/);
  assert.equal(accepted.headers.has('Authorization'), false);
  assert.equal(joining.w.sessionStorage.getItem('commons_token'), null);
  assert.equal(joining.w.sessionStorage.getItem('workspace_invitation'), null);
  assert.equal(joining.w.document.querySelector('#invitation-view').hidden, true);
  assert.notEqual(joining.w.document.querySelector('#modal-title').textContent, 'Save your sign-in');
  joining.w.document.querySelector('#signout').click(); await flush();
  assert.equal(joining.w.sessionStorage.getItem('commons_token'), null);
  assert.equal(joining.w.sessionStorage.getItem('workspace_invitation_claims'), null);
  assert.equal(joining.w.document.querySelector('#credential-recovery').hidden, true, 'sign-out discards pending credentials and claim secrets');
  joining.dom.window.close();

  const existing = setup(`http://127.0.0.1:8002/#invite=${code}`, 'existing-token'); await flush();
  assert.equal(existing.w.document.querySelector('#invitation-content [name=name]'), null);
  existing.w.document.querySelector('#invitation-content form').dispatchEvent(new existing.w.Event('submit', { bubbles: true, cancelable: true })); await flush();
  const reused = existing.requests.find(r => r.url === '/v1/invitations/accept');
  assert.equal(reused.body.code, code); assert.equal(reused.body.name, undefined); assert.match(reused.body.claim_secret, /^[0-9a-f]{64}$/);
  assert.equal(reused.headers.get('Authorization'), 'Bearer existing-token');
  assert.equal(existing.w.sessionStorage.getItem('commons_token'), 'existing-token');
  existing.dom.window.close();

  const late = setup(`http://127.0.0.1:8002/#invite=${code}`); await flush();
  const originalFetch = late.w.fetch; let release;
  late.w.fetch = (url, options) => url === '/v1/invitations/accept' ? new Promise(resolve => { release = async () => resolve(await originalFetch(url, options)); }) : originalFetch(url, options);
  late.w.document.querySelector('#invitation-content [name=name]').value = 'Alex';
  late.w.document.querySelector('#invitation-content [name=password]').value = 'chosen invitation password';
  if (late.w.document.querySelector('#invitation-content [name=confirm_password]')) late.w.document.querySelector('#invitation-content [name=confirm_password]').value = 'chosen invitation password';
  late.w.document.querySelector('#invitation-content form').dispatchEvent(new late.w.Event('submit', { bubbles: true, cancelable: true })); await flush();
  const newerCode = 'n'.repeat(43); late.w.sessionStorage.setItem('workspace_invitation', newerCode);
  await late.w.__invitationTest.openInvitation(newerCode); await release(); await flush();
  assert.equal(late.w.sessionStorage.getItem('commons_token'), null, 'late signup cannot replace the session');
  assert.equal(late.w.sessionStorage.getItem('workspace_invitation'), newerCode, 'late signup preserves newer invitation');
  assert.equal(late.w.document.querySelector('#invitation-view').hidden, false);
  assert.equal(late.w.document.querySelector('#credential-recovery').hidden, false, 'late one-time credentials remain saveable');
  assert.equal(JSON.parse(late.w.sessionStorage.getItem('workspace_invitation_claims'))[code].credentials.token, 'guest-token');
  late.dom.window.close();

  const logout = setup(`http://127.0.0.1:8002/#invite=${code}`); await flush();
  const logoutFetch = logout.w.fetch; let completeAfterLogout;
  logout.w.fetch = (url, options) => url === '/v1/invitations/accept' ? new Promise(resolve => { completeAfterLogout = async () => resolve(await logoutFetch(url, options)); }) : logoutFetch(url, options);
  logout.w.document.querySelector('#invitation-content [name=name]').value = 'Alex';
  logout.w.document.querySelector('#invitation-content [name=password]').value = 'chosen invitation password';
  if (logout.w.document.querySelector('#invitation-content [name=confirm_password]')) logout.w.document.querySelector('#invitation-content [name=confirm_password]').value = 'chosen invitation password';
  logout.w.document.querySelector('#invitation-content form').dispatchEvent(new logout.w.Event('submit', { bubbles: true, cancelable: true })); await flush();
  logout.w.document.querySelector('#signout').click(); await completeAfterLogout(); await flush();
  assert.equal(logout.w.sessionStorage.getItem('commons_token'), null);
  assert.equal(logout.w.sessionStorage.getItem('workspace_invitation_claims'), null, 'late responses cannot restore discarded recovery secrets');
  assert.equal(logout.w.document.querySelector('#invitation-view').hidden, true);
  logout.dom.window.close();

  const parsing = setup(); await flush(); parsing.w.__invitationTest.state.token = 'existing-token';
  const parsingFetch = parsing.w.fetch; let parsed;
  parsing.w.fetch = (url, options) => url === '/v1/me' ? Promise.resolve({ ok: true, status: 200, json: () => new Promise(resolve => { parsed = () => resolve(owner); }) }) : parsingFetch(url, options);
  const opening = parsing.w.__invitationTest.openInvitation(code); await flush();
  parsing.w.document.querySelector('#signout').click(); parsed(); await opening; await flush();
  assert.equal(parsing.w.__invitationTest.state.me, null, 'late identity body cannot overwrite sign-out');
  assert.equal(parsing.w.document.querySelector('#invitation-view').hidden, true);
  parsing.dom.window.close();

  const lost = setup(`http://127.0.0.1:8002/#invite=${code}`); await flush();
  const serverFetch = lost.w.fetch; let failed = false;
  lost.w.fetch = async (url, options) => {
    const response = await serverFetch(url, options);
    if (url === '/v1/invitations/accept' && !failed) { failed = true; throw new Error('Connection lost after commit'); }
    if (url === '/v1/invitations/preview' && failed) return { status: 200, ok: true, json: async () => ({ project, role: 'guest', accepted: true }) };
    return response;
  };
  lost.w.document.querySelector('#invitation-content [name=name]').value = 'Alex';
  lost.w.document.querySelector('#invitation-content [name=password]').value = 'chosen invitation password';
  if (lost.w.document.querySelector('#invitation-content [name=confirm_password]')) lost.w.document.querySelector('#invitation-content [name=confirm_password]').value = 'chosen invitation password';
  lost.w.document.querySelector('#invitation-content form').dispatchEvent(new lost.w.Event('submit', { bubbles: true, cancelable: true })); await flush();
  const firstClaim = JSON.parse(lost.w.sessionStorage.getItem('workspace_invitation_claims'))[code].body;
  assert.equal(lost.w.sessionStorage.getItem('commons_token'), null);
  await lost.w.__invitationTest.openInvitation(code);
  assert.equal(lost.w.document.querySelector('#invitation-content [name=name]').value, 'Alex');
  lost.w.document.querySelector('#invitation-content [name=password]').value = 'chosen invitation password';
  if (lost.w.document.querySelector('#invitation-content [name=confirm_password]')) lost.w.document.querySelector('#invitation-content [name=confirm_password]').value = 'chosen invitation password';
  lost.w.document.querySelector('#invitation-content form').dispatchEvent(new lost.w.Event('submit', { bubbles: true, cancelable: true })); await flush();
  assert.equal(lost.w.document.querySelector('#invitation-content [name=confirm_password]'), null);
  assert.ok(!JSON.stringify(firstClaim).includes('password'));
  const retry = lost.requests.filter(r => r.url === '/v1/invitations/accept')[1];
  assert.deepEqual(retry.body, firstClaim, 'recovery uses the persisted private operation secret and original body');
  assert.equal(lost.w.sessionStorage.getItem('commons_token'), null);
  lost.dom.window.close();
  process.stdout.write('Invitation DOM checks passed: roles, SSH steps, fragment secrecy, signup, existing identity, late responses and lost-response recovery.\n');
})().catch(error => { console.error(error); process.exit(1); });
