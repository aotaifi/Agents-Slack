#!/usr/bin/env node
'use strict';
// DOM-only account checks; use an isolated jsdom install through JSDOM_PATH.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { JSDOM } = process.env.JSDOM_PATH ? createRequire(path.resolve(process.env.JSDOM_PATH, 'package.json'))('jsdom') : require('jsdom');
const staticDir = path.resolve(__dirname, '../src/agent_commons/static');
const html = fs.readFileSync(path.join(staticDir, 'index.html'), 'utf8');
const source = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
const me = { id: 'human', name: 'Researcher', handle: 'researcher', kind: 'human', owner: null, has_password: true };
const password = '  exact password with spaces  ';
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const response = (data, status = 200) => ({ status, ok: status < 400, json: async () => structuredClone(data) });
async function create(transport = async () => response(null, 401), storage = {}) {
  const dom = new JSDOM(html, { url: 'https://workspace.test', runScripts: 'outside-only' }); const w = dom.window; const requests = [];
  w.Headers = Headers; w.crypto.randomUUID = require('node:crypto').randomUUID;
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; }; w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  for (const [key, value] of Object.entries(storage)) w.sessionStorage.setItem(key, value);
  w.fetch = async (url, options = {}) => { const request = { url, ...options, body: options.body ? JSON.parse(options.body) : null }; requests.push(request); if (url.startsWith('/v1/notifications?')) return response({ items: [], next_cursor: null, cursor: null, unread_count: 0 }); if (url === '/v1/projects') return response({ items: [] }); return transport(request); };
  w.eval(source.replace('  const savedToken = state.token;', '  window.__accountTest = { state, ui, signIn, passwordSignIn, restoreSession, showAccount, signOut, openInvitation, pollEvents };\n  const savedToken = state.token;'));
  await tick(); return { dom, w, requests, ...w.__accountTest };
}
function submit(w, form) { form.dispatchEvent(new w.Event('submit', { bubbles: true, cancelable: true })); }
(async () => {
  const boot = await create(req => req.url === '/v1/me' ? response(me) : response(null, 401));
  assert.equal(boot.state.me.id, 'human'); assert.equal(boot.state.token, ''); assert.equal(boot.ui.workspace.hidden, false);
  assert.equal(boot.requests[0].credentials, 'same-origin'); assert.equal(boot.requests[0].headers.has('Authorization'), false);
  boot.w.close();
  const signedout = await create(); assert.equal(signedout.state.me, null); assert.equal(signedout.w.document.querySelector('#signin-error').textContent, ''); signedout.w.close();
  let sessionMe = { ...me }; const account = await create(req => {
    if (req.url === '/v1/auth/login') return response({ actor: sessionMe });
    if (req.url === '/v1/me' && req.method === 'PATCH') { sessionMe = { ...sessionMe, name: req.body.name }; return response(sessionMe); }
    if (req.url === '/v1/auth/password') { sessionMe = { ...sessionMe, has_password: true }; return response({ actor: sessionMe }); }
    if (req.url === '/v1/auth/logout') return response(null, 204);
    return response(null, 401);
  });
  const d = account.w.document;
  assert.equal(d.querySelector('#login-handle').autocomplete, 'username'); assert.equal(d.querySelector('#login-password').autocomplete, 'current-password'); assert.equal(d.querySelector('#login-remember').checked, false);
  d.querySelector('#login-handle').value = me.handle; d.querySelector('#login-password').value = password; d.querySelector('#login-remember').checked = true;
  submit(account.w, d.querySelector('#password-signin-form')); await tick();
  const login = account.requests.find(r => r.url === '/v1/auth/login'); assert.deepEqual(login.body, { handle: me.handle, password, remember: true });
  assert.equal(account.state.me.id, 'human'); assert.equal(account.state.token, ''); assert.equal(account.w.sessionStorage.getItem('commons_token'), null); assert.equal(d.querySelector('#login-password').value, '');
  account.showAccount(); assert.equal(d.querySelector('#modal [name=username]').readOnly, true);
  account.state.messages = [{ id: 'own-message', text: 'Own finding', author: me, reply_to: null, reactions: [] }, { id: 'agent-message', text: 'Owned agent finding', author: { id: 'agent', name: 'Scout', kind: 'agent', owner: me }, reply_to: null, reactions: [] }];
  const profile = d.querySelector('#modal [name=name]').closest('form'); profile.querySelector('[name=name]').value = 'Updated researcher'; submit(account.w, profile); await tick(); assert.equal(account.state.me.name, 'Updated researcher'); assert.match(account.ui.identity.textContent, /Updated researcher/); assert.match(account.ui.messages.querySelector('.message-author').textContent, /Updated researcher/); assert.match(account.ui.messages.querySelectorAll('.badge')[1].textContent, /owned by Updated researcher/);
  let form = d.querySelector('.account-password'); assert.equal(form.querySelector('[name=password]').autocomplete, 'new-password');
  form.querySelector('[name=current_password]').value = 'old password stays exact'; form.querySelector('[name=password]').value = password; form.querySelector('[name=confirm_password]').value = password; submit(account.w, form); await tick();
  assert.deepEqual(account.requests.find(r => r.url === '/v1/auth/password').body, { password, current_password: 'old password stays exact', remember: false });
  assert.equal(account.state.token, ''); assert.equal(d.querySelector('.account-password [name=password]').value, '');
  account.w.sessionStorage.setItem('workspace_invitation_claims', '{}'); await account.signOut(); assert.equal(account.state.me, null); assert.equal(account.ui.signin.hidden, false); assert.equal(account.w.sessionStorage.getItem('workspace_invitation_claims'), null); account.w.close();
  const setup = await create(req => req.url === '/v1/me' ? response({ ...me, has_password: false }) : req.url === '/v1/auth/password' ? response({ actor: me }) : response(null, 401), { commons_token: 'human-backup-token' });
  assert.equal(setup.w.document.querySelector('#modal-title').textContent, 'My account'); form = setup.w.document.querySelector('.account-password'); assert.equal(form.querySelector('[name=current_password]'), null);
  form.querySelector('[name=password]').value = password; form.querySelector('[name=confirm_password]').value = password; submit(setup.w, form); await tick(); assert.equal(setup.requests.find(r => r.url === '/v1/auth/password').headers.get('Authorization'), 'Bearer human-backup-token'); assert.equal(setup.state.token, ''); assert.equal(setup.w.sessionStorage.getItem('commons_token'), null); setup.w.close();
  for (const hasPassword of [false, true]) {
    let rotatedActor = { ...me, has_password: hasPassword }; let finishRotation; const pendingPolls = [];
    const rotation = await create(req => {
      if (req.url === '/v1/me' && req.method === 'PATCH') { rotatedActor = { ...rotatedActor, name: req.body.name }; return response(rotatedActor); }
      if (req.url === '/v1/me') return response(rotatedActor);
      if (req.url === '/v1/auth/password') return new Promise(resolve => { finishRotation = () => { rotatedActor = { ...rotatedActor, has_password: true }; resolve(response({ actor: rotatedActor })); }; });
      if (req.url.includes('/events')) return new Promise(resolve => pendingPolls.push(() => resolve(response(null, 401))));
      return response(null, 401);
    });
    rotation.state.project = { id: 'project' }; rotation.showAccount();
    const beforeRotationPoll = rotation.pollEvents('project', rotation.state.projectGeneration); await tick();
    const passwordForm = rotation.w.document.querySelector('.account-password');
    if (hasPassword) passwordForm.querySelector('[name=current_password]').value = password;
    passwordForm.querySelector('[name=password]').value = password; passwordForm.querySelector('[name=confirm_password]').value = password;
    submit(rotation.w, passwordForm); await tick();
    pendingPolls.shift()(); await beforeRotationPoll;
    assert.equal(rotation.state.me.id, me.id, 'old-cookie 401 during pending rotation must not sign out');
    const duringRotationPoll = rotation.pollEvents('project', rotation.state.projectGeneration); await tick();
    finishRotation(); await tick();
    assert.equal(rotation.state.me.has_password, true); assert.equal(rotation.state.token, '');
    pendingPolls.shift()(); await duringRotationPoll;
    assert.equal(rotation.state.me.id, me.id, 'delayed old-cookie 401 after rotation must not sign out');
    assert.equal(rotation.ui.workspace.hidden, false);
    const freshProfile = rotation.w.document.querySelector('#modal [name=name]').closest('form'); freshProfile.querySelector('[name=name]').value = 'Name after password rotation'; submit(rotation.w, freshProfile); await tick();
    assert.equal(rotation.state.me.name, 'Name after password rotation', 'profile generation remains usable after rotation');
    rotation.w.close();
  }
  let release; const race = await create(req => {
    if (req.url === '/v1/auth/login') return new Promise(resolve => { release = () => resolve(response({ actor: me })); });
    if (req.url === '/v1/auth/logout') return response(null, 204);
    return response(null, 401);
  });
  const pendingLogin = race.passwordSignIn(me.handle, password); await tick(); const pendingLogout = race.signOut(); await tick(); assert.equal(race.requests.some(r => r.url === '/v1/auth/logout'), false, 'logout waits for late Set-Cookie login'); release(); await Promise.all([pendingLogin, pendingLogout]); assert.equal(race.requests.at(-1).url, '/v1/auth/logout'); assert.equal(race.state.me, null); race.w.close();
  const failedLogout = await create(req => req.url === '/v1/me' ? response(me) : response({ detail: 'offline' }, 503)); assert.equal(await failedLogout.signOut(), false); assert.equal(failedLogout.state.me.id, me.id); assert.match(failedLogout.ui.toast.textContent, /session may still be active/); failedLogout.w.close();
  const signup = await create(req => {
    if (req.url === '/v1/invitations/preview') return response({ project: { id: 'project', name: 'Study' }, role: 'guest', accepted: false });
    if (req.url === '/v1/invitations/accept') { const stored = signup.w.sessionStorage.getItem('workspace_invitation_claims'); assert.equal(stored.includes(password), false); assert.equal(stored.includes('"password"'), false); return response({ actor: me, token: 'backup-token', project: { id: 'project' } }); }
    if (req.url === '/v1/auth/login') return response({ actor: me });
    return response(null, 401);
  });
  await signup.openInvitation('invite-code'); const invitation = signup.w.document.querySelector('#invitation-content form'); invitation.querySelector('[name=name]').value = 'Researcher'; invitation.querySelector('[name=handle]').value = me.handle; invitation.querySelector('[name=password]').value = password; invitation.querySelector('[name=confirm_password]').value = password; invitation.querySelector('[name=remember]').checked = true; submit(signup.w, invitation); await tick(); await tick();
  const accept = signup.requests.find(r => r.url === '/v1/invitations/accept'); assert.equal(accept.body.password, password); assert.equal(accept.credentials, 'omit', 'new identity must exclude stale HttpOnly cookies'); assert.equal(signup.requests.find(r => r.url === '/v1/auth/login').body.remember, true); assert.equal(signup.state.me.id, me.id); assert.equal(signup.w.sessionStorage.getItem('commons_token'), null); assert.equal(signup.w.sessionStorage.getItem('workspace_invitation_claims').includes(password), false); assert.notEqual(signup.w.document.querySelector('#modal-title').textContent, 'Save your sign-in'); signup.w.close();
  const rejected = await create(req => req.url === '/v1/invitations/preview' ? response({ project: { id: 'project', name: 'Study' }, role: 'guest' }) : req.url === '/v1/invitations/accept' ? response({ actor: me, token: 'recovery-token', project: { id: 'project' } }) : response(null, 401));
  await rejected.openInvitation('failed-login'); const rejectedForm = rejected.w.document.querySelector('#invitation-content form'); rejectedForm.querySelector('[name=name]').value = me.name; rejectedForm.querySelector('[name=password]').value = password; rejectedForm.querySelector('[name=confirm_password]').value = password; submit(rejected.w, rejectedForm); await tick(); await tick();
  assert.equal(rejected.ui.signin.hidden, false, 'post-accept login error must be visible'); assert.equal(rejected.w.document.querySelector('#invitation-view').hidden, true); assert.match(rejected.w.document.querySelector('#signin-error').textContent, /incorrect/); assert.match(rejected.w.sessionStorage.getItem('workspace_invitation_claims'), /recovery-token/); assert.equal(rejected.w.sessionStorage.getItem('workspace_invitation_claims').includes(password), false); rejected.w.close();
  const existing = await create(req => req.url === '/v1/me' ? response(me) : req.url === '/v1/invitations/preview' ? response({ project: { id: 'project', name: 'Study' }, role: 'guest' }) : req.url === '/v1/invitations/accept' ? response({ actor: me, token: null, project: { id: 'project' } }) : response(null, 401)); await existing.openInvitation('existing-code'); assert.equal(existing.w.document.querySelector('#invitation-content [name=name]'), null); assert.equal(existing.w.document.querySelector('#invitation-content [name=password]'), null); submit(existing.w, existing.w.document.querySelector('#invitation-content form')); await tick(); await tick(); assert.equal(existing.requests.find(r => r.url === '/v1/invitations/accept').credentials, 'same-origin', 'existing identity preserves cookie'); existing.w.close();
  const resumed = await create(req => {
    if (req.url === '/v1/invitations/preview') return response({ project: { id: 'project', name: 'Study' }, role: 'guest', accepted: true });
    if (req.url === '/v1/invitations/accept') return response({ actor: me, token: 'same-recovered-token', project: { id: 'project' } });
    if (req.url === '/v1/auth/login') return response({ actor: me });
    return response(null, 401); // Expired HttpOnly cookie fails identity lookup.
  }, { workspace_invitation_claims: JSON.stringify({ 'resume-code': { existingActorId: null, body: { code: 'resume-code', claim_secret: 'private-secret', name: me.name, handle: me.handle } } }) });
  await resumed.openInvitation('resume-code'); const resumedForm = resumed.w.document.querySelector('#invitation-content form'); assert.equal(resumedForm.querySelector('[name=confirm_password]'), null); resumedForm.querySelector('[name=password]').value = password; submit(resumed.w, resumedForm); await tick(); await tick();
  const recovered = resumed.requests.find(r => r.url === '/v1/invitations/accept'); assert.equal(recovered.credentials, 'omit', 'new-identity claim recovery excludes expired cookie'); assert.equal(recovered.body.password, undefined, 'claim recovery never changes the password'); assert.equal(resumed.requests.find(r => r.url === '/v1/auth/login').credentials, 'same-origin'); assert.equal(resumed.state.me.id, me.id); resumed.w.close();
  console.log('Account DOM checks passed: cookie boot, signed-out boot, password login/remember, profile, password setup/change, signup secrecy, existing identity, logout errors, late-login/logout ordering and password rotation 401 races.');
})().catch(error => { console.error(error); process.exitCode = 1; });
