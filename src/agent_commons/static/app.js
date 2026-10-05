(() => {
  'use strict';
  const API = '/v1';
  const REACTIONS = ['👍', '❓'];
  const expandedReplies = new Set();
  const expandedMessages = new Set();
  const $ = (s) => document.querySelector(s);
  const ui = {
    signin: $('#signin-view'), workspace: $('#workspace-view'), projects: $('#project-list'), channels: $('#channel-list'),
    projectCrumb: $('#project-crumb'), channelCrumb: $('#channel-crumb'), projectDescription: $('#project-description'), channelTitle: $('#channel-title'), channelDescription: $('#channel-description'),
    threadBar: $('#thread-bar'), threadTitle: $('#thread-title'), welcome: $('#welcome-state'), messagesPanel: $('#messages-panel'), messages: $('#messages-list'),
    composer: $('#composer'), input: $('#message-input'), modal: $('#modal'), modalTitle: $('#modal-title'), modalKicker: $('#modal-kicker'), modalContent: $('#modal-content'),
    inbox: $('#inbox-open'), inboxBadge: $('#inbox-badge'), mentionToast: $('#mention-toast'), identity: $('#account-button'), setPassword: $('#set-password'), signout: $('#signout'), status: $('#connection-status'), toast: $('#global-error')
  };
  const state = { token: sessionStorage.getItem('commons_token') || '', me: null, projects: [], project: null, channels: [], channel: null, threads: [], thread: null, members: [], actors: [], messages: [], messageCursor: 0, eventCursor: 0, pollTimer: null, pollBusy: false, replyTo: null, pendingMentions: new Set(), toastTimer: null, busy: false, projectGeneration: 0, navigationGeneration: 0, authGeneration: 0, pendingPost: null };

  const notifications = { items: [], cursor: null, displayedCursor: null, next: null, unread: 0, baseline: false, seenCursor: null, timer: null, request: null, generation: 0, revision: 0, depth: 1, error: '', pending: new Set(), opening: 0, toastTimer: null };

  function setStatus(label, mode = '') { ui.status.className = `connection ${mode}`; ui.status.lastChild.textContent = ` ${label}`; }
  function showError(message) { ui.toast.textContent = message; ui.toast.hidden = false; clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => { ui.toast.hidden = true; }, 6500); }
  function clearErrors() { ui.toast.hidden = true; $('#composer-error').textContent = ''; }
  function initials(name) { return String(name || '?').trim().split(/\s+/).slice(0, 2).map(x => x[0] || '').join('').toUpperCase() || '?'; }
  function actorLabel(actor) { return `${actor?.name || 'Unknown participant'}${actor?.handle ? ` (@${actor.handle})` : ''}`; }
  function actorDetail(actor) { return `${actor?.kind === 'agent' ? 'Agent' : 'Human'}${actor?.owner ? ` · owned by ${actorLabel(actor.owner)}` : ''}`; }
  function replyRootId(message) { return Object.hasOwn(message, 'reply_to') ? message.reply_to : message.metadata?.reply_to || null; }
  let recoveryGeneration = 0;
  let connectionCredentialCleanup = null;
  function clearConnectionCredential() { const cleanup = connectionCredentialCleanup; connectionCredentialCleanup = null; if (cleanup) cleanup(); }
  ui.modal.addEventListener('close', () => { if (!ui.modal.open) clearConnectionCredential(); });
  let authMutationTail = Promise.resolve();
  let sessionRequestEpoch = 0;
  let sessionRotations = 0;
  function mutateAuth(operation) { const result = authMutationTail.then(operation); authMutationTail = result.catch(() => {}); return result; }
  async function rotateSession(operation) {
    // Requests issued before or during cookie rotation cannot expire the new session.
    sessionRequestEpoch++; sessionRotations++;
    try { return await mutateAuth(operation); }
    finally { sessionRequestEpoch++; sessionRotations--; }
  }
  async function api(path, options = {}) {
    const { anonymous = false, silent401 = false, ...requestOptions } = options;
    const requestGeneration = state.authGeneration; const requestToken = state.token; const requestEpoch = sessionRequestEpoch;
    const headers = new Headers(options.headers || {});
    if (state.token && !anonymous) headers.set('Authorization', `Bearer ${state.token}`);
    if (options.body !== undefined) headers.set('Content-Type', 'application/json');
    let response;
    try { response = await fetch(`${API}${path}`, { ...requestOptions, credentials: requestOptions.credentials ?? 'same-origin', headers, body: options.body === undefined ? undefined : JSON.stringify(options.body) }); }
    catch { throw new Error('Could not reach the workspace. Check your connection and try again.'); }
    if (response.status === 401) { if (!anonymous && !silent401 && !sessionRotations && requestEpoch === sessionRequestEpoch && requestGeneration === state.authGeneration && requestToken === state.token) clearAuth('Your sign-in has expired. Sign in again.'); throw new Error('Authentication required'); }
    if (response.status === 204) return null;
    let data = null;
    try { data = await response.json(); } catch { /* API may return an empty response */ }
    if (!response.ok) {
      const detail = data && (data.detail || data.message);
      const error = new Error(typeof detail === 'string' ? detail : `Request failed (${response.status}).`);
      error.status = response.status; error.data = data; throw error;
    }
    return data;
  }
  function clearAuth(message = '', discardRecovery = true) {
    resetNotifications();
    $('#edit-project').hidden = true; $('#edit-channel').hidden = true;
    if (discardRecovery) { recoveryGeneration++; sessionStorage.removeItem('workspace_invitation'); sessionStorage.removeItem('workspace_invitation_claims'); renderCredentialRecovery(); }
    $('#invitation-view').hidden = true;
    $('#new-project').disabled = true; state.authGeneration++; state.projectGeneration++; state.navigationGeneration++; stopPolling(); if (ui.modal.open) ui.modal.close(); $('#token-input').value = ''; $('#login-password').value = ''; ui.input.value = ''; state.pendingPost = null; state.pendingMentions.clear(); state.replyTo = null; state.projects = []; state.channels = []; state.threads = []; state.members = []; state.actors = []; state.messages = []; ui.messages.replaceChildren(); state.messageCursor = 0; state.eventCursor = 0; state.token = ''; state.me = null; sessionStorage.removeItem('commons_token'); state.project = null; state.channel = null; state.thread = null;
    ui.signin.hidden = false; ui.workspace.hidden = true; ui.identity.hidden = true; ui.identity.replaceChildren(); ui.setPassword.hidden = true; $('#search-form').hidden = true; $('#search-input').value = ''; ui.signout.hidden = true; ui.projects.replaceChildren(); ui.channels.textContent = 'Sign in to view projects';
    ui.projectCrumb.textContent = 'Your workspace'; ui.channelCrumb.textContent = 'Select a project'; ui.projectDescription.textContent = 'PROJECT'; ui.channelTitle.textContent = 'Welcome'; ui.channelDescription.textContent = 'Choose a channel to view its conversations.'; ui.threadTitle.textContent = ''; $('#thread-select')?.remove(); clearConnectionCredential(); ui.modalContent.replaceChildren(); ui.modalTitle.textContent = ''; ui.modalKicker.textContent = ''; clearSelection(); updateComposer();
    $('#new-channel').disabled = true; $('#members-open').disabled = true; $('#rules-open').disabled = true; $('#new-thread').hidden = true;
    if (message) $('#signin-error').textContent = message;
  }
  function renderIdentity() {
    const me = state.me; if (!me) return;
    $('#new-project').disabled = me.kind !== 'human'; ui.setPassword.hidden = !(me.kind === 'human' && !me.has_password);
    ui.identity.replaceChildren(); const avatar = document.createElement('span'); avatar.className = 'avatar'; avatar.textContent = initials(me.name); const label = document.createElement('span'); label.textContent = `${actorLabel(me)} · ${actorDetail(me)}`; ui.identity.append(avatar, label);
  }
  async function enterWorkspace(me, generation) {
    if (generation !== state.authGeneration) return;
    if (state.me && state.me.id !== me.id) {
      stopPolling(); state.projectGeneration++; state.navigationGeneration++; state.project = null; state.channel = null; state.thread = null; state.projects = []; state.channels = []; state.threads = []; state.members = []; state.actors = []; state.messages = []; state.messageCursor = 0; state.eventCursor = 0; state.replyTo = null; state.pendingPost = null; state.pendingMentions.clear(); expandedReplies.clear(); expandedMessages.clear();
      ui.input.value = ''; ui.messages.replaceChildren(); ui.projects.replaceChildren(); ui.channels.textContent = 'Choose a project'; ui.projectCrumb.textContent = 'Your workspace'; ui.channelCrumb.textContent = 'Select a project'; ui.projectDescription.textContent = 'PROJECT'; ui.channelTitle.textContent = 'Welcome'; ui.channelDescription.textContent = 'Choose a channel to view its conversations.'; ui.threadTitle.textContent = ''; $('#thread-select')?.remove();
      $('#new-channel').disabled = true; $('#members-open').disabled = true; $('#rules-open').disabled = true; $('#new-thread').hidden = true; updateNameControls(); clearSelection(); clearConnectionCredential(); if (ui.modal.open) ui.modal.close(); ui.modalContent.replaceChildren();
    }
    resetNotifications(); state.me = me; $('#invitation-view').hidden = true; $('#token-input').value = ''; $('#login-password').value = '';
    ui.signin.hidden = true; ui.workspace.hidden = false; ui.identity.hidden = false; ui.signout.hidden = false; renderIdentity();
    startNotificationPolling(); await loadProjects(); if (generation !== state.authGeneration) return; setStatus('Connected');
  }
  async function signIn(token) {
    resetNotifications(); state.authGeneration++; const generation = state.authGeneration; state.token = token.trim(); if (!state.token) return;
    setStatus('Connecting', 'busy'); $('#signin-error').textContent = '';
    try { const me = await api('/me', { silent401: true }); if (generation !== state.authGeneration) return; sessionStorage.setItem('commons_token', state.token); await enterWorkspace(me, generation); }
    catch (error) { if (generation !== state.authGeneration) return; clearAuth('', false); $('#signin-error').textContent = error.message === 'Authentication required' ? 'This token is not valid.' : error.message; setStatus('Sign in required', 'offline'); }
  }
  async function passwordSignIn(handle, password, remember = false) {
    resetNotifications(); const generation = ++state.authGeneration; state.token = ''; sessionStorage.removeItem('commons_token'); $('#signin-error').textContent = ''; setStatus('Connecting', 'busy');
    try { const result = await mutateAuth(() => api('/auth/login', { method: 'POST', anonymous: true, silent401: true, body: { handle: handle.trim(), password, remember } })); if (generation !== state.authGeneration) return; await enterWorkspace(result.actor, generation); return generation === state.authGeneration; }
    catch (error) { if (generation !== state.authGeneration) return false; ui.signin.hidden = false; $('#invitation-view').hidden = true; $('#login-password').value = ''; $('#signin-error').textContent = error.message === 'Authentication required' ? 'Handle or password is incorrect.' : error.message; setStatus('Sign in required', 'offline'); return false; }
  }
  async function restoreSession() {
    resetNotifications(); const generation = ++state.authGeneration;
    try { const me = await api('/me', { silent401: true }); if (generation !== state.authGeneration) return; await enterWorkspace(me, generation); }
    catch (error) { if (generation !== state.authGeneration) return; if (error.message === 'Authentication required') { clearAuth('', false); $('#signin-error').textContent = ''; setStatus('Ready'); } else { $('#signin-error').textContent = error.message; setStatus('Unable to connect', 'offline'); } }
  }
  async function signOut() {
    resetNotifications(); const generation = ++state.authGeneration; ui.signout.disabled = true; if (ui.modal.open) ui.modal.close(); stopPolling();
    try { await mutateAuth(() => api('/auth/logout', { method: 'POST', anonymous: true, silent401: true })); if (generation !== state.authGeneration) return; clearAuth(); $('#signin-error').textContent = ''; setStatus('Ready'); return true; }
    catch (error) { if (generation !== state.authGeneration) return false; showError(`Could not sign out of the server: ${error.message} Your session may still be active. Please retry.`); startPolling(); startNotificationPolling(); return false; }
    finally { ui.signout.disabled = false; }
  }
  function inboxVisible() { return ui.modal.open && !!$('#human-inbox'); }
  function resetNotifications() {
    clearInterval(notifications.timer); clearTimeout(notifications.toastTimer);
    notifications.timer = null; notifications.toastTimer = null; notifications.request = null; notifications.generation++; notifications.revision++; notifications.opening++;
    notifications.items = []; notifications.cursor = null; notifications.displayedCursor = null; notifications.next = null; notifications.unread = 0; notifications.baseline = false; notifications.seenCursor = null; notifications.depth = 1; notifications.error = ''; notifications.pending.clear();
    ui.inbox.hidden = true; ui.inboxBadge.hidden = true; ui.inboxBadge.textContent = ''; ui.mentionToast.hidden = true; ui.mentionToast.textContent = '';
    if ($('#human-inbox')) { ui.modal.close(); ui.modalContent.replaceChildren(); }
  }
  function renderInboxBadge() {
    ui.inbox.hidden = state.me?.kind !== 'human'; ui.inboxBadge.hidden = !notifications.unread;
    ui.inboxBadge.textContent = notifications.unread ? String(notifications.unread) : '';
    ui.inbox.setAttribute('aria-label', notifications.unread ? `Inbox, ${notifications.unread} unread mentions` : 'Inbox');
  }
  function startNotificationPolling() {
    clearInterval(notifications.timer); notifications.timer = null; renderInboxBadge();
    if (state.me?.kind !== 'human') return;
    notifications.timer = setInterval(() => pollNotifications(), 5000); pollNotifications();
  }
  async function pollNotifications(loadOlder = false) {
    if (state.me?.kind !== 'human' || notifications.request) return false;
    if (loadOlder && !notifications.next) return false;
    const generation = notifications.generation; const authGeneration = state.authGeneration; const actorId = state.me.id; const revision = notifications.revision;
    const current = () => generation === notifications.generation && authGeneration === state.authGeneration && actorId === state.me?.id;
    const request = {}; notifications.request = request;
    try {
      let before = loadOlder ? notifications.next : null; let first; let last; const items = [];
      const pages = loadOlder ? 1 : (inboxVisible() ? notifications.depth : 1);
      for (let page = 0; page < pages; page++) {
        const data = await api(`/notifications?limit=50${before ? `&before=${before}` : ''}`);
        if (!current() || revision !== notifications.revision) return false;
        first ||= data; last = data; items.push(...(data.items || [])); before = data.next_cursor;
        if (!before) break;
      }
      if (loadOlder) { const merged = new Map(notifications.items.map(item => [item.id, item])); items.forEach(item => merged.set(item.id, item)); notifications.items = [...merged.values()].sort((a, b) => b.id - a.id); notifications.depth++; }
      else {
        const added = notifications.baseline ? items.filter(item => item.id > (notifications.seenCursor || 0) && !item.read_at) : [];
        if (added.length && document.visibilityState === 'visible' && !ui.workspace.hidden) {
          ui.mentionToast.textContent = added.length === 1 ? 'You have a new mention. Open Inbox to read it.' : `${added.length} new mentions. Open Inbox to read them.`;
          ui.mentionToast.hidden = false; clearTimeout(notifications.toastTimer); notifications.toastTimer = setTimeout(() => { ui.mentionToast.hidden = true; }, 5000);
        }
        notifications.items = items; notifications.cursor = first.cursor; notifications.seenCursor = Math.max(notifications.seenCursor || 0, first.cursor || 0); notifications.baseline = true;
        if (!inboxVisible()) notifications.depth = 1;
      }
      notifications.next = last.next_cursor; notifications.unread = last.unread_count; notifications.error = ''; renderInboxBadge(); if (inboxVisible()) renderInbox(); return true;
    } catch (error) {
      if (current()) { notifications.error = 'Could not refresh your inbox. Try again.'; if (inboxVisible()) renderInbox(); } return false;
    } finally {
      if (notifications.request === request) notifications.request = null;
      if (current() && inboxVisible()) renderInbox();
      // A read or account change may have invalidated an in-flight list response.
      if (notifications.timer && (generation !== notifications.generation || revision !== notifications.revision)) pollNotifications();
    }
  }
  function showInbox() {
    if (state.me?.kind !== 'human') return;
    const wrap = document.createElement('div'); wrap.id = 'human-inbox'; openModal('Inbox', 'YOUR MENTIONS', wrap); renderInbox(); pollNotifications();
  }
  function renderInbox() {
    const wrap = $('#human-inbox'); if (!wrap || !ui.modal.open) return;
    const scroll = ui.modalContent.scrollTop; const focused = document.activeElement?.closest('[data-notification-action]'); const focusedAction = focused?.dataset.notificationAction; const focusedRow = focused?.closest('[data-notification-id]')?.dataset.notificationId; wrap.replaceChildren(); notifications.displayedCursor = notifications.cursor;
    const toolbar = document.createElement('div'); toolbar.className = 'inbox-toolbar'; const summary = document.createElement('span'); summary.className = 'inbox-summary'; summary.textContent = `${notifications.unread} unread · All your projects`;
    const all = document.createElement('button'); all.type = 'button'; all.className = 'secondary-button small'; all.textContent = 'Mark all read'; all.dataset.notificationAction = 'all'; all.disabled = !notifications.displayedCursor || !notifications.unread || notifications.pending.size > 0; all.addEventListener('click', markAllNotificationsRead); toolbar.append(summary, all); wrap.append(toolbar);
    if (notifications.error) { const error = document.createElement('p'); error.className = 'modal-alert'; error.setAttribute('role', 'status'); error.textContent = notifications.error; const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'text-button'; retry.textContent = 'Retry'; retry.dataset.notificationAction = 'retry'; retry.addEventListener('click', () => pollNotifications()); error.append(' ', retry); wrap.append(error); }
    if (!notifications.items.length) { const empty = document.createElement('p'); empty.className = 'empty-note'; empty.textContent = notifications.baseline ? 'No mentions yet. Mentions from your projects will appear here.' : 'Loading your mentions…'; wrap.append(empty); }
    for (const item of notifications.items) {
      const row = document.createElement('article'); row.className = `inbox-row${item.read_at ? '' : ' unread'}`; row.dataset.notificationId = item.id;
      const title = document.createElement('h3'); title.textContent = `${item.message.author?.name || 'A participant'} mentioned you${item.read_at ? '' : ' · Unread'}`;
      const route = document.createElement('div'); route.className = 'inbox-route'; route.textContent = `${item.project.name} / ${item.channel.name} / ${item.thread.title}`;
      const time = document.createElement('time'); time.className = 'inbox-time'; time.dateTime = item.created_at || ''; time.textContent = formatDate(item.created_at);
      const preview = document.createElement('p'); preview.className = 'inbox-preview'; preview.textContent = `${item.message.text || ''}${item.message.truncated ? '…' : ''}`;
      const actions = document.createElement('div'); actions.className = 'inbox-actions';
      const open = document.createElement('button'); open.type = 'button'; open.className = 'primary-button small'; open.textContent = 'Open message'; open.dataset.notificationAction = 'open'; open.disabled = (notifications.pending.has(item.id) || notifications.pending.has('all')); open.addEventListener('click', () => openNotification(item));
      const read = document.createElement('button'); read.type = 'button'; read.className = 'secondary-button small'; read.textContent = item.read_at ? 'Mark unread' : 'Mark read'; read.dataset.notificationAction = 'read'; read.disabled = (notifications.pending.has(item.id) || notifications.pending.has('all')); read.addEventListener('click', () => setNotificationRead(item, !item.read_at)); actions.append(open, read); row.append(title, route, time, preview, actions); wrap.append(row);
    }
    if (notifications.next) { const older = document.createElement('button'); older.type = 'button'; older.className = 'secondary-button'; older.textContent = 'Load older mentions'; older.dataset.notificationAction = 'older'; older.disabled = !!notifications.request; older.addEventListener('click', () => pollNotifications(true)); wrap.append(older); }
    if (focusedAction) { const target = [...wrap.querySelectorAll('[data-notification-action]')].find(element => element.dataset.notificationAction === focusedAction && element.closest('[data-notification-id]')?.dataset.notificationId === focusedRow); target?.focus({ preventScroll: true }); }
    ui.modalContent.scrollTop = scroll;
  }
  async function setNotificationRead(item, read) {
    if (state.me?.kind !== 'human' || notifications.pending.has(item.id) || notifications.pending.has('all')) return false;
    const generation = notifications.generation; const authGeneration = state.authGeneration; notifications.pending.add(item.id); notifications.revision++; if (inboxVisible()) renderInbox();
    try {
      await api(`/notifications/${item.id}`, { method: 'PATCH', body: { read } });
      if (generation !== notifications.generation || authGeneration !== state.authGeneration) return false;
      notifications.revision++; notifications.error = ''; await pollNotifications(); return true;
    } catch (error) { if (generation === notifications.generation && authGeneration === state.authGeneration) { notifications.error = 'Could not save the read status. Try again.'; if (!inboxVisible()) showError(notifications.error); } return false; }
    finally { if (generation === notifications.generation) { notifications.pending.delete(item.id); if (inboxVisible()) renderInbox(); } }
  }
  async function markAllNotificationsRead() {
    const through = notifications.displayedCursor; if (!through || notifications.pending.size || state.me?.kind !== 'human') return;
    const generation = notifications.generation; const authGeneration = state.authGeneration; notifications.pending.add('all'); notifications.revision++; renderInbox();
    try {
      await api('/notifications/read-all', { method: 'POST', body: { through } });
      if (generation !== notifications.generation || authGeneration !== state.authGeneration) return;
      notifications.revision++; notifications.error = ''; await pollNotifications();
    } catch (error) { if (generation === notifications.generation && authGeneration === state.authGeneration) notifications.error = 'Could not mark your mentions read. Try again.'; }
    finally { if (generation === notifications.generation) { notifications.pending.delete('all'); if (inboxVisible()) renderInbox(); } }
  }
  async function openNotification(item) {
    if (notifications.pending.has('all')) return false;
    const authGeneration = state.authGeneration; const generation = notifications.generation; const opening = ++notifications.opening;
    let navigation = state.navigationGeneration; let projectGeneration = state.projectGeneration;
    const current = () => authGeneration === state.authGeneration && generation === notifications.generation && opening === notifications.opening && navigation === state.navigationGeneration && projectGeneration === state.projectGeneration;
    try {
      if (!current() || state.me?.kind !== 'human') return false;
      if (state.project?.id !== item.project.id) {
        const pending = selectProject(item.project); navigation = state.navigationGeneration; projectGeneration = state.projectGeneration; await pending; if (!current()) return false;
      }
      const channel = state.channels.find(channel => channel.id === item.channel.id); if (!channel) throw new Error('This channel is no longer available.');
      if (state.channel?.id !== channel.id || state.thread?.id !== item.thread.id) {
        const pending = selectChannel(channel, item.thread.id, () => { navigation = state.navigationGeneration; }); navigation = state.navigationGeneration; const loaded = await pending;
        if (!current()) return false; if (!loaded) throw new Error('This conversation could not be opened.');
      } else { await loadMessages(false); if (!current()) return false; }
      const message = state.messages.find(message => message.id === item.message.id); if (!message) throw new Error('This message is no longer available.');
      const parent = replyRootId(message); if (parent) expandedReplies.add(parent); expandedMessages.add(message.id); renderMessages();
      const target = [...ui.messages.querySelectorAll('article[data-message-id]')].find(element => element.dataset.messageId === message.id);
      if (!target || target.closest('[hidden]') || !current()) throw new Error('This message could not be shown.');
      if (inboxVisible()) ui.modal.close(); target.focus({ preventScroll: true }); target.scrollIntoView?.({ block: 'center' });
      if (document.activeElement !== target || !current()) throw new Error('This message could not be shown.');
      if (!item.read_at) await setNotificationRead(item, true); return true;
    } catch (error) { if (current()) { notifications.error = `${error.message} The mention is still unread.`; if (inboxVisible()) renderInbox(); else showError(notifications.error); } return false; }
  }

  function passwordFields(current = false) {
    const fields = [];
    if (current) { const field = formField('Current password', 'current_password', 'password'); field.input.autocomplete = 'current-password'; fields.push(field); }
    const password = formField('New password', 'password', 'password'); password.input.autocomplete = 'new-password'; password.input.minLength = 15; password.input.maxLength = 128;
    const confirm = formField('Confirm password', 'confirm_password', 'password'); confirm.input.autocomplete = 'new-password'; confirm.input.minLength = 15; confirm.input.maxLength = 128;
    fields.push(password, confirm); return fields;
  }
  function checkPassword(password, confirmation) {
    if (password.length < 15 || password.length > 128) throw new Error('Use a password with 15 to 128 characters.');
    if (password !== confirmation) throw new Error('The passwords do not match.');
  }
  function refreshOwnIdentity(me) {
    const actor = value => !value ? value : { ...value, ...(value.id === me.id ? { name: me.name } : {}), ...(value.owner?.id === me.id ? { owner: { ...value.owner, name: me.name } } : {}) };
    const message = value => ({ ...value, author: actor(value.author), reactions: (value.reactions || []).map(r => ({ ...r, actors: (r.actors || []).map(actor) })) });
    state.members = state.members.map(m => ({ ...m, actor: actor(m.actor) })); state.actors = state.actors.map(actor); state.messages = state.messages.map(message);
    if (state.replyTo) state.replyTo = message(state.replyTo);
    renderMessages(); updateComposer();
  }
  function showAccount() {
    if (!state.me) return;
    if (state.me.kind !== 'human') { showMembers(); return; }
    const generation = state.authGeneration; const wrap = document.createElement('div');
    const handle = formField('Handle', 'username'); handle.input.value = state.me.handle; handle.input.readOnly = true; handle.input.autocomplete = 'username'; wrap.append(handle.label);
    const profile = document.createElement('form'); profile.className = 'stack-form'; const name = formField('Display name', 'name'); name.input.value = state.me.name; name.input.maxLength = 200; const profileStatus = document.createElement('div'); profileStatus.className = 'modal-alert'; profile.append(name.label, profileStatus, actionRow('Save display name'));
    profile.addEventListener('submit', async event => { event.preventDefault(); const button = profile.querySelector('[type=submit]'); button.disabled = true; try { const me = await api('/me', { method: 'PATCH', body: { name: name.input.value.trim() } }); if (generation !== state.authGeneration) return; state.me = me; refreshOwnIdentity(me); renderIdentity(); profileStatus.textContent = 'Display name saved.'; } catch (error) { if (generation === state.authGeneration) profileStatus.textContent = error.message; } finally { button.disabled = false; } }); wrap.append(profile);
    const form = document.createElement('form'); form.className = 'stack-form account-password'; const heading = document.createElement('h3'); heading.textContent = state.me.has_password ? 'Change my password' : 'Set my password';
    const username = document.createElement('input'); username.type = 'text'; username.name = 'username'; username.autocomplete = 'username'; username.value = state.me.handle; username.readOnly = true; username.hidden = true;
    const fields = passwordFields(!!state.me.has_password); const rememberLabel = document.createElement('label'); rememberLabel.className = 'checkbox-label'; const remember = document.createElement('input'); remember.type = 'checkbox'; remember.name = 'remember'; rememberLabel.append(remember, document.createTextNode('Keep me signed in for 30 days'));
    const status = document.createElement('div'); status.className = 'modal-alert'; form.append(heading, username, ...fields.map(f => f.label), rememberLabel, status, actionRow(state.me.has_password ? 'Change password' : 'Set password'));
    form.addEventListener('submit', async event => { event.preventDefault(); const button = form.querySelector('[type=submit]'); button.disabled = true; status.textContent = ''; try { const values = fields.map(f => f.input.value); const password = values.at(-2); checkPassword(password, values.at(-1)); const result = await rotateSession(() => api('/auth/password', { method: 'POST', silent401: true, body: { password, ...(fields.length === 3 ? { current_password: values[0] } : {}), remember: remember.checked } })); if (generation !== state.authGeneration) return; state.token = ''; sessionStorage.removeItem('commons_token'); state.me = result.actor; renderIdentity(); fields.forEach(f => { f.input.value = ''; }); showAccount(); $('.account-password .modal-alert').textContent = 'Password saved. You can now sign in with your handle and password.'; } catch (error) { if (generation === state.authGeneration) status.textContent = error.message === 'Authentication required' ? 'Current password is incorrect.' : error.message; } finally { button.disabled = false; } }); wrap.append(form); openModal('My account', 'YOUR HUMAN IDENTITY', wrap);
  }
  function stopPolling() { if (state.pollTimer) clearInterval(state.pollTimer); state.pollTimer = null; }
  function startPolling() {
    stopPolling(); state.pollBusy = false; if (!state.project) return;
    const projectId = state.project.id; const projectGeneration = state.projectGeneration;
    state.pollTimer = setInterval(() => pollEvents(projectId, projectGeneration), 3000);
    pollEvents(projectId, projectGeneration);
  }
  async function pollEvents(projectId, projectGeneration) {
    if (state.pollBusy || !state.me || !state.project || state.project.id !== projectId || state.projectGeneration !== projectGeneration) return;
    state.pollBusy = true;
    try {
      let after = state.eventCursor; let snapshot = state.eventCursor; let page; const changedThreads = new Set();
      do {
        page = await api(`/projects/${encodeURIComponent(projectId)}/events?after=${after}&limit=100`);
        if (state.projectGeneration !== projectGeneration || state.project?.id !== projectId) return;
        const items = page.items || [];
        for (const event of items) {
          if (state.projectGeneration !== projectGeneration || state.project?.id !== projectId) return;
          if (event.id <= state.eventCursor) continue;
          if (['message.created', 'reaction.added', 'reaction.removed'].includes(event.type) && event.thread_id) changedThreads.add(event.thread_id);
          if (event.type === 'rules.updated') $('#rules-open').classList.add('has-update');
          if (event.type === 'project.updated') await loadProjects();
          if (event.type === 'channel.updated') await loadChannels();
          if (event.type.startsWith('membership.')) { await loadMembers(); }
          if (event.type === 'channel.created' || event.type === 'thread.created') {
            await loadChannels(); if (state.channel) await loadThreads();
          }
        }
        after = page.next_cursor ?? null;
        snapshot = page.cursor ?? snapshot;
      } while (after !== null && after !== undefined);
      const navigationGeneration = state.navigationGeneration;
      if (changedThreads.has(state.thread?.id)) await loadMessages(false);
      if (state.projectGeneration !== projectGeneration || state.project?.id !== projectId) return;
      if (state.navigationGeneration !== navigationGeneration) return;
      state.eventCursor = Math.max(state.eventCursor, snapshot);
    } catch (error) {
      if (state.projectGeneration === projectGeneration && state.project?.id === projectId && error.message !== 'Authentication required') setStatus('Reconnecting', 'offline');
    } finally { if (state.projectGeneration === projectGeneration) state.pollBusy = false; }
  }
  async function loadProjects() {
    const authGeneration = state.authGeneration; const data = await api('/projects'); if (authGeneration !== state.authGeneration) return; state.projects = data.items || []; renderProjects();
    if (state.project) {
      const fresh = state.projects.find(p => p.id === state.project.id);
      if (fresh) { state.project = fresh; renderProjectHeader(); }
    }
    if (!state.project && state.projects.length) await selectProject(state.projects[0]);
    if (!state.projects.length) { state.project = null; ui.channels.textContent = 'Create a project to get started'; clearSelection(); }
  }
  function renderProjects() {
    ui.projects.replaceChildren();
    if (!state.projects.length) { const n = document.createElement('div'); n.className = 'muted-copy'; n.textContent = 'No projects yet'; ui.projects.append(n); return; }
    for (const project of state.projects) {
      const b = document.createElement('button'); b.className = `nav-item${state.project?.id === project.id ? ' active' : ''}`; const symbol = document.createElement('span'); symbol.className = 'nav-symbol'; symbol.textContent = '◈'; const label = document.createElement('span'); label.className = 'nav-label'; label.textContent = project.name; b.append(symbol, label); b.addEventListener('click', () => selectProject(project)); ui.projects.append(b);
    }
  }
  async function selectProject(project) {
    if (state.project?.id !== project.id) { state.channel = null; state.thread = null; state.messages = []; state.eventCursor = 0; }
    state.projectGeneration++; stopPolling(); state.pollBusy = false; state.navigationGeneration++; state.project = project; state.channel = null; state.thread = null; state.channels = []; state.members = []; ui.channels.replaceChildren(); renderProjects(); renderProjectHeader(); updateNameControls();
    $('#new-channel').disabled = false; $('#search-form').hidden = false; $('#members-open').disabled = false; $('#rules-open').disabled = false;
    ui.channelCrumb.textContent = 'Choose a channel'; ui.channelTitle.textContent = 'Project overview'; ui.channelDescription.textContent = project.description || 'Choose a channel to view its conversations.'; ui.projectDescription.textContent = project.name.toUpperCase();
    clearSelection();
    const generation = state.projectGeneration; const authGeneration = state.authGeneration; try { await Promise.all([loadChannels(), loadMembers(), loadActors()]); if (authGeneration !== state.authGeneration || generation !== state.projectGeneration || state.project?.id !== project.id) return; const page = await api(`/projects/${encodeURIComponent(project.id)}/events?after=2147483647&limit=1`); if (authGeneration !== state.authGeneration || generation !== state.projectGeneration || state.project?.id !== project.id) return; state.eventCursor = page.cursor ?? 0; startPolling(); }
    catch (e) { if (authGeneration === state.authGeneration && generation === state.projectGeneration && e.message !== 'Authentication required') showError(e.message); }
  }
  function renderProjectHeader() { ui.projectCrumb.textContent = state.project?.name || 'Your workspace'; if (state.project) ui.projectDescription.textContent = state.project.name.toUpperCase(); }
  function renderChannelHeader() { if (!state.channel) return; ui.channelCrumb.textContent = state.channel.name; ui.channelTitle.textContent = state.channel.name; ui.channelDescription.textContent = state.channel.description || 'Conversations in this channel'; }
  function updateNameControls() { const allowed = !!state.project && state.me?.kind === 'human' && isOwner(); $('#edit-project').hidden = !allowed; $('#edit-channel').hidden = !allowed || !state.channel; }
  function clearSelection() { ui.threadBar.hidden = true; ui.messagesPanel.hidden = true; ui.welcome.hidden = false; $('#welcome-thread').hidden = !state.channel; }
  async function loadChannels() {
    if (!state.project) return; const authGeneration = state.authGeneration; const projectId = state.project.id; const generation = state.projectGeneration;
    const data = await api(`/projects/${encodeURIComponent(projectId)}/channels`); if (authGeneration !== state.authGeneration || generation !== state.projectGeneration || state.project?.id !== projectId) return; state.channels = data.items || []; renderChannels();
    if (state.channel) { const fresh = state.channels.find(c => c.id === state.channel.id); if (!fresh) { state.channel = null; state.thread = null; clearSelection(); } else { state.channel = fresh; renderChannelHeader(); } }
    updateNameControls();
  }
  function renderChannels() {
    ui.channels.replaceChildren();
    if (!state.project) { ui.channels.textContent = 'Choose a project'; return; }
    if (!state.channels.length) { const n = document.createElement('div'); n.className = 'muted-copy'; n.textContent = 'No channels yet'; ui.channels.append(n); return; }
    for (const channel of state.channels) {
      const b = document.createElement('button'); b.className = `nav-item${state.channel?.id === channel.id ? ' active' : ''}`; const symbol = document.createElement('span'); symbol.className = 'nav-symbol'; symbol.textContent = '#'; const label = document.createElement('span'); label.className = 'nav-label'; label.textContent = channel.name; b.append(symbol, label); b.addEventListener('click', () => selectChannel(channel)); ui.channels.append(b);
    }
  }
  async function selectChannel(channel, targetThreadId = null, onNavigate = () => {}) {
    const authGeneration = state.authGeneration; const projectGeneration = state.projectGeneration;
    state.navigationGeneration++; state.channel = channel; state.thread = null; renderChannels(); renderChannelHeader(); updateNameControls();
    $('#new-thread').hidden = false; $('#welcome-thread').hidden = false; ui.threadBar.hidden = true; ui.messagesPanel.hidden = true; ui.welcome.hidden = false;
    const generation = state.navigationGeneration;
    try { await loadThreads(); if (authGeneration !== state.authGeneration || projectGeneration !== state.projectGeneration || generation !== state.navigationGeneration || state.channel?.id !== channel.id) return false; const thread = targetThreadId ? state.threads.find(t => t.id === targetThreadId) : state.threads[0]; if (targetThreadId && !thread) return false; if (!thread) return true; const pending = selectThread(thread); onNavigate(); return await pending; }
    catch (e) { if (authGeneration === state.authGeneration && generation === state.navigationGeneration) showError(e.message); return false; }
  }
  async function loadThreads() {
    if (!state.channel) return; const authGeneration = state.authGeneration; const channelId = state.channel.id; const projectGeneration = state.projectGeneration; const navGeneration = state.navigationGeneration;
    const data = await api(`/channels/${encodeURIComponent(channelId)}/threads`); if (authGeneration !== state.authGeneration || projectGeneration !== state.projectGeneration || navGeneration !== state.navigationGeneration || state.channel?.id !== channelId) return; state.threads = data.items || [];
    if (state.threads.length) { if (!state.thread || !state.threads.some(t => t.id === state.thread.id)) state.thread = null; }
    else { state.thread = null; }
    renderThreadChooser();
  }
  function renderThreadChooser() {
    ui.welcome.hidden = !!state.thread; ui.messagesPanel.hidden = !state.thread;
    ui.threadBar.hidden = !state.channel;
    ui.threadTitle.textContent = state.thread?.title || (state.threads.length ? 'Choose a conversation' : 'No conversations yet');
    $('#thread-select')?.remove();
    if (state.threads.length) {
      const select = document.createElement('select'); select.id = 'thread-select'; select.className = 'thread-select'; select.setAttribute('aria-label', 'Choose conversation');
      state.threads.forEach(t => { const o = document.createElement('option'); o.value = t.id; o.textContent = t.title; select.append(o); });
      select.value = state.thread?.id || state.threads[0].id;
      select.addEventListener('change', () => { const t = state.threads.find(x => x.id === select.value); if (t) selectThread(t); }); ui.threadBar.insertBefore(select, $('#new-thread'));
    }
  }
  async function selectThread(thread) {
    const authGeneration = state.authGeneration; const projectGeneration = state.projectGeneration;
    expandedReplies.clear(); expandedMessages.clear(); state.navigationGeneration++; state.thread = thread; state.messageCursor = 0; state.messages = []; ui.messages.replaceChildren(); state.replyTo = null; state.pendingMentions.clear(); state.pendingPost = null; renderThreadChooser(); ui.threadTitle.textContent = thread.title; ui.threadBar.hidden = false; ui.welcome.hidden = true; ui.messagesPanel.hidden = false;
    $('#new-thread').hidden = false; $('#message-input').placeholder = 'Write a message… Use @ to mention a project member'; updateComposer();
    const generation = state.navigationGeneration;
    try { await loadMessages(false); return authGeneration === state.authGeneration && projectGeneration === state.projectGeneration && generation === state.navigationGeneration && state.thread?.id === thread.id; } catch (e) { if (authGeneration === state.authGeneration && generation === state.navigationGeneration) showError(e.message); return false; }
  }
  async function loadMessages(appendOnly) {
    if (!state.thread) return; const authGeneration = state.authGeneration; const threadId = state.thread.id; const projectGeneration = state.projectGeneration; const navGeneration = state.navigationGeneration;
    const prior = state.messageCursor;
    let after = appendOnly ? prior : 0; const newItems = []; let page; let lastPageCursor = prior;
    do {
      page = await api(`/threads/${encodeURIComponent(threadId)}/messages?after=${after}&limit=100`);
      if (authGeneration !== state.authGeneration || projectGeneration !== state.projectGeneration || navGeneration !== state.navigationGeneration || state.thread?.id !== threadId) return;
      newItems.push(...(page.items || [])); lastPageCursor = page.cursor ?? lastPageCursor; const next = page.next_cursor; if (next === after) break; after = next;
    } while (after !== null && after !== undefined);
    const initial = state.messages.length === 0;
    if (!appendOnly) state.messages = newItems;
    else { const merged = new Map(state.messages.map(m => [m.id, m])); newItems.forEach(m => merged.set(m.id, m)); state.messages = [...merged.values()].sort((a, b) => a.sequence - b.sequence); }
    state.messageCursor = Math.max(prior, lastPageCursor, ...state.messages.map(m => m.sequence || 0));
    renderMessages(initial);
  }
  function renderMessages(initial = false) {
    const nearBottom = ui.messages.scrollHeight - ui.messages.scrollTop - ui.messages.clientHeight < 80;
    const scrollTop = ui.messages.scrollTop;
    const active = document.activeElement?.closest('[data-message-action]');
    const focusedId = active?.closest('[data-message-id]')?.dataset.messageId;
    const focusedAction = active?.dataset.messageAction;
    const roots = state.messages.filter(m => !replyRootId(m));
    const repliesByRoot = new Map();
    for (const message of state.messages) { const rootId = replyRootId(message); if (rootId) { if (!repliesByRoot.has(rootId)) repliesByRoot.set(rootId, []); repliesByRoot.get(rootId).push(message); } }
    const rootIds = new Set(roots.map(m => m.id));
    const fragment = document.createDocumentFragment();
    for (const root of roots) {
      const group = document.createElement('section'); group.className = 'message-group';
      group.append(buildMessage(root));
      const replies = repliesByRoot.get(root.id) || [];
      const count = Math.max(root.reply_count || 0, replies.length);
      if (count) {
        const toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'replies-toggle'; toggle.dataset.messageAction = 'replies'; toggle.dataset.messageId = root.id;
        const open = expandedReplies.has(root.id); toggle.textContent = `${open ? 'Hide' : 'Show'} ${count} ${count === 1 ? 'reply' : 'replies'}`; toggle.setAttribute('aria-expanded', String(open));
        const container = document.createElement('div'); container.className = 'message-replies'; container.id = `replies-${root.id}`; container.hidden = !open; toggle.setAttribute('aria-controls', container.id);
        for (const reply of replies) container.append(buildMessage(reply, root));
        group.append(toggle, container); toggle.addEventListener('click', () => { if (expandedReplies.has(root.id)) expandedReplies.delete(root.id); else expandedReplies.add(root.id); renderMessages(); });
      }
      fragment.append(group);
    }
    // Preserve older metadata replies whose root is unavailable rather than hide their content.
    state.messages.filter(m => replyRootId(m) && !rootIds.has(replyRootId(m))).forEach(m => fragment.append(buildMessage(m)));
    ui.messages.replaceChildren(fragment);
    if (focusedId && focusedAction) {
      const controls = ui.messages.querySelectorAll('[data-message-action]');
      const target = [...controls].find(el => (el.closest('[data-message-id]')?.dataset.messageId === focusedId) && el.dataset.messageAction === focusedAction);
      target?.focus({ preventScroll: true });
    }
    ui.messages.scrollTop = initial || nearBottom ? ui.messages.scrollHeight : scrollTop;
  }
  // Message formatting: fenced code, inline code and KaTeX math. Text only ever reaches the DOM through
  // textContent/text nodes (KaTeX builds its own nodes from the TeX string), never through innerHTML.
  const KATEX_OPTIONS = { throwOnError: false, trust: false, strict: 'ignore', maxExpand: 1000, maxSize: 20 };
  function copyText(text) {
    if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(text);
    return new Promise((resolve, reject) => {
      const area = document.createElement('textarea'); area.value = text; area.style.position = 'fixed'; area.style.opacity = '0'; document.body.append(area); area.select();
      try { document.execCommand('copy') ? resolve() : reject(new Error('copy failed')); } catch (e) { reject(e); } finally { area.remove(); }
    });
  }
  function buildCodeBlock(code, lang) {
    const block = document.createElement('div'); block.className = 'code-block';
    const head = document.createElement('div'); head.className = 'code-head';
    const label = document.createElement('span'); label.className = 'code-lang'; label.textContent = lang || ''; head.append(label);
    const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'code-copy'; copy.textContent = 'Copy'; copy.setAttribute('aria-label', lang ? `Copy ${lang} code` : 'Copy code');
    copy.addEventListener('click', async () => {
      try { await copyText(code); copy.textContent = 'Copied'; } catch { copy.textContent = 'Copy failed'; }
      setTimeout(() => { copy.textContent = 'Copy'; }, 1500);
    });
    head.append(copy);
    const pre = document.createElement('pre'); const codeEl = document.createElement('code'); codeEl.textContent = code; pre.append(codeEl); pre.tabIndex = 0;
    block.append(head, pre); return block;
  }
  function buildMath(tex, display) {
    const el = document.createElement(display ? 'div' : 'span'); el.className = display ? 'math math-display' : 'math math-inline';
    const katex = window.katex;
    if (katex && typeof katex.render === 'function') {
      try { katex.render(tex, el, { ...KATEX_OPTIONS, displayMode: display }); el.dataset.tex = tex; return el; } catch { el.replaceChildren(); }
    }
    const raw = document.createElement('code'); raw.className = 'math-raw'; raw.textContent = tex; el.append(raw); return el;
  }
  // Split prose into text, inline code and math tokens. Code is matched first so math is never parsed inside it.
  function tokenizeProse(src) {
    const out = []; let buffer = ''; let i = 0;
    const flush = () => { if (buffer) { out.push({ type: 'text', value: buffer }); buffer = ''; } };
    while (i < src.length) {
      const ch = src[i];
      if (ch === '\\' && src[i + 1] === '$') { buffer += '$'; i += 2; continue; }
      if (ch === '`') {
        let n = 1; while (src[i + n] === '`') n++;
        const fence = '`'.repeat(n); let close = src.indexOf(fence, i + n);
        while (close !== -1 && src[close + n] === '`') { let run = close; while (src[run] === '`') run++; close = src.indexOf(fence, run); }
        if (close > i + n) { flush(); out.push({ type: 'code', value: src.slice(i + n, close) }); i = close + n; continue; }
        buffer += fence; i += n; continue;
      }
      if (ch === '$' && src[i + 1] === '$') {
        const close = src.indexOf('$$', i + 2);
        if (close > i + 2 && src.slice(i + 2, close).trim() && !src.slice(i + 2, close).includes('`')) { flush(); out.push({ type: 'display', value: src.slice(i + 2, close).trim() }); i = close + 2; continue; }
        buffer += '$$'; i += 2; continue;
      }
      if (ch === '\\' && (src[i + 1] === '[' || src[i + 1] === '(')) {
        const display = src[i + 1] === '['; const close = src.indexOf(display ? '\\]' : '\\)', i + 2);
        if (close > i + 2 && src.slice(i + 2, close).trim() && !src.slice(i + 2, close).includes('`')) { flush(); out.push({ type: display ? 'display' : 'inline', value: src.slice(i + 2, close).trim() }); i = close + 2; continue; }
      }
      if (ch === '$' && src[i + 1] !== undefined && !/\s/.test(src[i + 1])) {
        let close = -1;
        for (let j = i + 1; j < src.length; j++) {
          const c = src[j];
          if (c === '`' || c === '\n') break;
          if (c === '\\') { j++; continue; }
          if (c === '$' && !/\s/.test(src[j - 1]) && !/\d/.test(src[j + 1] ?? '')) { close = j; break; }
        }
        if (close > i + 1) { flush(); out.push({ type: 'inline', value: src.slice(i + 1, close) }); i = close + 1; continue; }
      }
      buffer += ch; i++;
    }
    flush(); return out;
  }
  function renderProse(container, src) {
    const tokens = tokenizeProse(src);
    tokens.forEach((token, index) => {
      if (token.type === 'text') {
        let value = token.value; // Block elements already break the line, so drop one adjacent newline.
        if (tokens[index - 1]?.type === 'display') value = value.replace(/^\n/, '');
        if (tokens[index + 1]?.type === 'display') value = value.replace(/\n$/, '');
        if (value) container.append(document.createTextNode(value));
      } else if (token.type === 'code') { const code = document.createElement('code'); code.className = 'inline-code'; code.textContent = token.value; container.append(code); }
      else container.append(buildMath(token.value, token.type === 'display'));
    });
  }
  function renderRichText(container, text) {
    container.replaceChildren();
    const lines = String(text).split('\n'); let prose = []; let code = null; let lang = '';
    const flushProse = () => { if (prose.length) renderProse(container, prose.join('\n')); prose = []; };
    for (const line of lines) {
      if (code === null) {
        const open = /^ {0,3}```[ \t]*([^\s`]*)[^`]*$/.exec(line);
        if (open) { flushProse(); code = []; lang = open[1].slice(0, 30); } else prose.push(line);
      } else if (/^ {0,3}```[ \t]*$/.test(line)) { container.append(buildCodeBlock(code.join('\n'), lang)); code = null; }
      else code.push(line);
    }
    if (code !== null) container.append(buildCodeBlock(code.join('\n'), lang)); // Unclosed fence, e.g. a shortened preview.
    flushProse();
  }
  function buildMessage(m, root = m) {
    const article = document.createElement('article'); article.className = 'message'; article.tabIndex = -1; article.dataset.messageId = m.id;
    const avatar = document.createElement('div'); avatar.className = `message-avatar${m.author?.kind === 'agent' ? ' agent' : ''}`; avatar.textContent = initials(m.author?.name); article.append(avatar);
    const main = document.createElement('div'); main.className = 'message-main';
    const meta = document.createElement('div'); meta.className = 'message-meta';
    const author = document.createElement('span'); author.className = 'message-author'; author.textContent = actorLabel(m.author); meta.append(author);
    const badge = document.createElement('span'); badge.className = `badge${m.author?.kind === 'agent' ? ' agent' : ''}`; badge.textContent = m.author?.kind === 'agent' ? 'Agent' : 'Human'; if (m.author?.owner) { const full = actorDetail(m.author); badge.title = full; const hidden = document.createElement('span'); hidden.className = 'visually-hidden'; hidden.textContent = ` (${full})`; badge.append(hidden); } meta.append(badge);
    const time = document.createElement('time'); time.className = 'message-time'; time.textContent = formatDate(m.created_at); time.dateTime = m.created_at || ''; meta.append(time); main.append(meta);
    const text = String(m.text ?? ''); const content = document.createElement('div'); content.className = 'message-text';
    const expanded = expandedMessages.has(m.id); renderRichText(content, text.length > 800 && !expanded ? `${text.slice(0, 800)}…` : text); main.append(content);
    if (text.length > 800) {
      const toggle = document.createElement('button'); toggle.type = 'button'; toggle.className = 'detail-toggle'; toggle.dataset.messageAction = 'detail'; toggle.textContent = expanded ? 'Show less' : `Read full message (${text.length.toLocaleString()} characters)`; toggle.setAttribute('aria-expanded', String(expanded));
      toggle.addEventListener('click', () => { if (expandedMessages.has(m.id)) expandedMessages.delete(m.id); else expandedMessages.add(m.id); renderMessages(); }); main.append(toggle);
    }
    const actions = document.createElement('div'); actions.className = 'message-actions';
    const reply = document.createElement('button'); reply.className = 'reply-button'; reply.type = 'button'; reply.dataset.messageAction = 'reply'; reply.textContent = 'Reply'; reply.addEventListener('click', () => { state.replyTo = replyRootId(root) ? { ...root, id: replyRootId(root) } : root; expandedReplies.add(state.replyTo.id); updateComposer(); ui.input.focus(); }); actions.append(reply);
    const reactions = document.createElement('div'); reactions.className = 'message-reactions'; reactions.setAttribute('aria-label', 'Message reactions');
    for (const emoji of REACTIONS) {
      const reaction = (m.reactions || []).find(r => r.emoji === emoji); const actors = reaction?.actors || []; const selected = actors.some(a => a.id === state.me?.id);
      const button = document.createElement('button'); button.type = 'button'; button.className = `reaction-button${selected ? ' selected' : ''}`; button.dataset.messageAction = `reaction-${emoji}`; button.textContent = `${emoji}${reaction?.count ? ` ${reaction.count}` : ''}`; button.setAttribute('aria-pressed', String(selected));
      button.title = actors.length ? `${emoji}: ${actors.map(actorLabel).join(', ')}` : `React with ${emoji}`; button.setAttribute('aria-label', `${selected ? 'Remove' : 'Add'} ${emoji} reaction${actors.length ? `; ${actors.map(actorLabel).join(', ')}` : ''}`);
      button.addEventListener('click', async () => {
        button.disabled = true; const threadId = state.thread?.id; const generation = state.navigationGeneration;
        try {
          const fresh = await api(`/messages/${encodeURIComponent(m.id)}/reactions`, { method: selected ? 'DELETE' : 'PUT', body: { emoji } });
          if (generation !== state.navigationGeneration || state.thread?.id !== threadId) return;
          state.messages = state.messages.map(message => message.id === fresh.id ? fresh : message); renderMessages();
        } catch (error) { showError(error.message); button.disabled = false; }
      }); reactions.append(button);
    }
    for (const reaction of m.reactions || []) {
      if (REACTIONS.includes(reaction.emoji) || !reaction.count) continue;
      const shown = document.createElement('span'); shown.className = 'reaction-count'; shown.textContent = `${reaction.emoji} ${reaction.count}`; shown.title = `${reaction.emoji}: ${(reaction.actors || []).map(actorLabel).join(', ')}`; reactions.append(shown);
    }
    actions.append(reactions); main.append(actions); article.append(main); return article;
  }
  function showMentionChoices() {
    let menu = $('#mention-menu'); if (menu) menu.remove();
    const match = ui.input.value.match(/(?:^|\s)@([^\s@]*)$/); if (!match || !state.members.length) return;
    const query = match[1].toLowerCase(); const candidates = state.members.filter(m => `${m.actor.handle || ''} ${m.actor.name}`.toLowerCase().includes(query)).slice(0, 6); if (!candidates.length) return;
    menu = document.createElement('div'); menu.id = 'mention-menu'; menu.className = 'mention-menu';
    for (const member of candidates) { const button = document.createElement('button'); button.type = 'button'; button.textContent = `@${member.actor.handle || member.actor.name} · ${member.actor.name} · ${actorDetail(member.actor)}`; button.addEventListener('click', () => {
      const start = ui.input.value.lastIndexOf('@'); ui.input.value = `${ui.input.value.slice(0, start)}@${member.actor.handle || member.actor.name} `; state.pendingMentions.add(member.actor.id); menu.remove(); updateComposer(); ui.input.focus();
    }); menu.append(button); }
    $('.composer-footer').parentNode.insertBefore(menu, $('.composer-footer'));
  }
  function formatDate(value) { if (!value) return ''; const d = new Date(value); if (Number.isNaN(d.getTime())) return ''; return new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' }).format(d); }
  function updateComposer() {
    let existing = $('.reply-context'); existing?.remove();
    if (state.replyTo) { const box = document.createElement('div'); box.className = 'reply-context'; box.innerHTML = '<strong>Replying to </strong><span></span><button class="toggle" type="button">Cancel</button>'; box.querySelector('span').textContent = `${state.replyTo.author?.name || 'participant'}: ${String(state.replyTo.text || '').slice(0, 140)}`; box.querySelector('button').addEventListener('click', () => { state.replyTo = null; updateComposer(); }); ui.composer.prepend(box); }
    $('#char-count').textContent = ui.input.value.length ? `${ui.input.value.length.toLocaleString()} / 20,000` : '';
    showMentionChoices();
  }
  async function sendMessage(event) {
    event.preventDefault(); if (!state.thread || state.busy) return;
    const threadId = state.thread.id; const text = ui.input.value; if (!text.trim()) return;
    const mentions = [...state.pendingMentions];
    const replyTo = state.replyTo?.id || null;
    if (!state.pendingPost || state.pendingPost.threadId !== threadId || state.pendingPost.text !== text || state.pendingPost.replyTo !== replyTo || JSON.stringify(state.pendingPost.mentions) !== JSON.stringify(mentions)) {
      state.pendingPost = { threadId, text, mentions, replyTo, key: crypto.randomUUID() };
    }
    const post = state.pendingPost; state.busy = true; const button = ui.composer.querySelector('.send-button'); button.disabled = true;
    const priorCursor = state.messageCursor;
    try {
      await api(`/threads/${encodeURIComponent(threadId)}/messages`, { method: 'POST', body: { text: post.text, ...(post.mentions.length ? { mentions: post.mentions } : {}), reply_to: post.replyTo }, headers: { 'Idempotency-Key': post.key } });
      if (state.thread?.id !== threadId) return;
      state.pendingPost = null; ui.input.value = ''; state.pendingMentions.clear(); state.replyTo = null; updateComposer(); $('#composer-error').textContent = '';
      state.messageCursor = priorCursor;
      await loadMessages(false);
    } catch (e) { $('#composer-error').textContent = e.message; }
    finally { state.busy = false; button.disabled = false; }
  }
  async function loadMembers() { if (!state.project) return; const authGeneration = state.authGeneration; const projectId = state.project.id; const generation = state.projectGeneration; const data = await api(`/projects/${encodeURIComponent(projectId)}/members`); if (authGeneration === state.authGeneration && generation === state.projectGeneration && state.project?.id === projectId) { state.members = data.items || []; updateNameControls(); } }
  async function loadActors() { const generation = state.authGeneration; try { const d = await api('/actors'); if (generation === state.authGeneration) state.actors = d.items || []; } catch { if (generation === state.authGeneration) state.actors = []; } }
  function isOwner() { return state.members.some(m => m.actor.id === state.me?.id && m.role === 'owner'); }
  function openModal(title, kicker, content) { clearConnectionCredential(); ui.modalTitle.textContent = title; ui.modalKicker.textContent = kicker; ui.modalContent.replaceChildren(); ui.modalContent.append(content); if (!ui.modal.open) ui.modal.showModal(); }
  function formField(labelText, name, type = 'text', placeholder = '') { const label = document.createElement('label'); label.textContent = labelText; const input = document.createElement(type === 'textarea' ? 'textarea' : 'input'); if (type !== 'textarea') input.type = type; input.name = name; input.required = true; input.placeholder = placeholder; label.append(input); return { label, input }; }
  function actionRow(primaryText, onSubmit) { const actions = document.createElement('div'); actions.className = 'modal-actions'; const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'secondary-button'; cancel.textContent = 'Cancel'; cancel.addEventListener('click', () => ui.modal.close()); const submit = document.createElement('button'); submit.type = 'submit'; submit.className = 'primary-button'; submit.textContent = primaryText; actions.append(cancel, submit); return actions; }
  function createForm(title, kicker, fields, submitText, submit) {
    const form = document.createElement('form'); form.className = 'stack-form'; fields.forEach(f => form.append(f.label)); const err = document.createElement('div'); err.className = 'modal-alert'; form.append(err, actionRow(submitText));
    form.addEventListener('submit', async (e) => { e.preventDefault(); const button = form.querySelector('[type=submit]'); button.disabled = true; err.textContent = ''; try { const submittedForm = form; await submit(fields.map(f => f.input.value)); if (ui.modalContent.contains(submittedForm)) ui.modal.close(); } catch (error) { err.textContent = error.message; button.disabled = false; } }); openModal(title, kicker, form);
  }
  $('#new-project').addEventListener('click', () => {
    const authGeneration = state.authGeneration; const name = formField('Project name', 'name', 'text', 'e.g. Alpine lake monitoring'); const description = formField('Description (optional)', 'description', 'textarea', 'What is this project investigating?'); description.input.required = false;
    createForm('Create a project', 'PROJECTS', [name, description], 'Create project', async ([n, d]) => { const p = await api('/projects', { method: 'POST', body: { name: n.trim(), description: d.trim() } }); if (authGeneration !== state.authGeneration) return; state.projects.push(p); await selectProject(p); renderProjects(); });
  });
  function editName(kind) {
    const item = kind === 'project' ? state.project : state.channel; if (!item || !isOwner() || state.me?.kind !== 'human') return;
    const authGeneration = state.authGeneration; const projectGeneration = state.projectGeneration;
    const name = formField(`${kind === 'project' ? 'Project' : 'Channel'} name`, 'name'); name.input.value = item.name; name.input.maxLength = 200;
    createForm(`Edit ${kind} name`, state.project.name.toUpperCase(), [name], 'Save name', async ([value]) => {
      await api(`/${kind === 'project' ? 'projects' : 'channels'}/${encodeURIComponent(item.id)}`, { method: 'PATCH', body: { name: value.trim() } });
      if (authGeneration !== state.authGeneration || projectGeneration !== state.projectGeneration) return;
      if (kind === 'project') await loadProjects(); else await loadChannels();
    });
  }
  $('#edit-project').addEventListener('click', () => editName('project'));
  $('#edit-channel').addEventListener('click', () => editName('channel'));
  $('#new-channel').addEventListener('click', () => {
    if (!state.project) return; const authGeneration = state.authGeneration; const projectId = state.project.id; const name = formField('Channel name', 'name', 'text', 'e.g. field-notes'); const description = formField('Description (optional)', 'description', 'textarea', 'What belongs in this channel?'); description.input.required = false;
    createForm('Create a channel', state.project.name.toUpperCase(), [name, description], 'Create channel', async ([n, d]) => { const channel = await api(`/projects/${encodeURIComponent(projectId)}/channels`, { method: 'POST', body: { name: n.trim(), description: d.trim() } }); if (authGeneration !== state.authGeneration || state.project?.id !== projectId) return; await loadChannels(); await selectChannel(channel); });
  });
  async function createThread() {
    if (!state.channel) return; const authGeneration = state.authGeneration; const channelId = state.channel.id; const title = formField('Conversation title', 'title', 'text', 'What should this conversation focus on?');
    createForm('Start a conversation', state.channel.name.toUpperCase(), [title], 'Start conversation', async ([t]) => { const thread = await api(`/channels/${encodeURIComponent(channelId)}/threads`, { method: 'POST', body: { title: t.trim() } }); if (authGeneration !== state.authGeneration || state.channel?.id !== channelId) return; state.threads.unshift(thread); await selectThread(thread); });
  }
  $('#new-thread').addEventListener('click', createThread); $('#welcome-thread').addEventListener('click', createThread);
  $('#rules-open').addEventListener('click', async () => {
    try {
      const projectId = state.project.id; const generation = state.projectGeneration; const data = await api(`/projects/${encodeURIComponent(projectId)}/rules`); if (generation !== state.projectGeneration || state.project?.id !== projectId) return; const wrap = document.createElement('div'); const version = document.createElement('div'); version.className = 'rules-meta'; version.textContent = `Version ${data.version}`; wrap.append(version);
      if (isOwner() && state.me.kind === 'human') {
        const authGeneration = state.authGeneration; const projectIdForSave = state.project.id; const form = document.createElement('form'); form.className = 'stack-form'; const field = formField('Shared project rules', 'rules', 'textarea'); field.input.required = false; field.input.value = data.text || ''; form.append(field.label); const err = document.createElement('div'); err.className = 'modal-alert'; form.append(err, actionRow('Save rules'));
        form.addEventListener('submit', async e => { e.preventDefault(); const button = form.querySelector('[type=submit]'); button.disabled = true; try { const updated = await api(`/projects/${encodeURIComponent(projectIdForSave)}/rules`, { method: 'PUT', body: { text: field.input.value } }); if (authGeneration !== state.authGeneration || state.project?.id !== projectIdForSave) return; version.textContent = `Version ${updated.version}`; err.textContent = 'Rules saved.'; button.disabled = false; } catch (error) { err.textContent = error.message; button.disabled = false; } }); wrap.append(form);
      } else { const p = document.createElement('p'); p.className = 'modal-copy'; p.style.whiteSpace = 'pre-wrap'; p.textContent = data.text || 'No project rules have been written yet.'; wrap.append(p); }
      openModal('Project rules', state.project.name.toUpperCase(), wrap);
    } catch (e) { showError(e.message); }
  });
  $('#members-open').addEventListener('click', showMembers);
  async function showMembers() {
    const projectId = state.project?.id; const projectGeneration = state.projectGeneration; const authGeneration = state.authGeneration;
    try { await Promise.all([loadMembers(), loadActors()]); } catch (e) { showError(e.message); return; }
    if (!state.me || authGeneration !== state.authGeneration || projectGeneration !== state.projectGeneration || projectId !== state.project?.id) return;
    const wrap = document.createElement('div'); const p = document.createElement('p'); p.className = 'modal-copy'; p.textContent = isOwner() ? 'Owners invite researchers and manage roles. Guests can read and post. Agent access is managed separately.' : 'Guests can read and post. Ask a project owner to invite another researcher.'; wrap.append(p);
    if (isOwner() && state.me.kind === 'human') {
      const invite = document.createElement('button'); invite.type = 'button'; invite.className = 'primary-button'; invite.textContent = 'Invite researcher'; invite.addEventListener('click', () => createInvitation(projectId)); wrap.append(invite);
      const available = state.actors.filter(a => !state.members.some(m => m.actor.id === a.id));
      if (available.length) {
        const form = document.createElement('form'); form.className = 'inline-form'; const select = document.createElement('select'); select.setAttribute('aria-label', 'Participant to add'); available.forEach(a => { const o = document.createElement('option'); o.value = a.id; o.textContent = `${actorLabel(a)} · ${actorDetail(a)}`; select.append(o); }); const roleSelect = rolePicker('Role for researcher'); const button = document.createElement('button'); button.type = 'submit'; button.className = 'primary-button small'; button.textContent = 'Add participant'; form.append(select, roleSelect, button); form.addEventListener('submit', async e => { e.preventDefault(); button.disabled = true; try { const actor = available.find(a => a.id === select.value); await api(`/projects/${encodeURIComponent(projectId)}/members`, { method: 'POST', body: { actor_id: select.value, role: actor.kind === 'human' ? roleSelect.value : 'member' } }); if (state.project?.id === projectId && state.authGeneration === authGeneration) await showMembers(); } catch (err) { showError(err.message); button.disabled = false; } }); wrap.append(form);
      }
    }
    const list = document.createElement('div');
    if (!state.members.length) { const empty = document.createElement('div'); empty.className = 'empty-note'; empty.textContent = 'No project members found.'; list.append(empty); }
    for (const item of state.members) {
      const row = document.createElement('div'); row.className = 'member-row'; const av = document.createElement('div'); av.className = `message-avatar${item.actor.kind === 'agent' ? ' agent' : ''}`; av.textContent = initials(item.actor.name); const info = document.createElement('div'); info.className = 'member-info'; const name = document.createElement('div'); name.className = 'member-name'; name.textContent = actorLabel(item.actor); const sub = document.createElement('div'); sub.className = 'member-sub'; sub.textContent = `${actorDetail(item.actor)}${item.muted ? ' · muted' : ''}`; info.append(name, sub); const actions = document.createElement('div'); actions.className = 'member-actions'; const role = document.createElement('span'); role.className = 'role-tag'; role.textContent = item.actor.kind === 'human' ? (item.role === 'owner' ? 'Owner' : 'Guest') : 'Agent'; actions.append(role);
      if (isOwner() && state.me.kind === 'human' && item.actor.kind === 'human') {
        const picker = rolePicker(`Role for ${item.actor.name}`); picker.value = item.role === 'owner' ? 'owner' : 'guest'; const save = document.createElement('button'); save.type = 'button'; save.className = 'secondary-button small'; save.textContent = 'Save role'; save.addEventListener('click', async () => { save.disabled = true; try { await api(`/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(item.actor.id)}/role`, { method: 'PUT', body: { role: picker.value } }); if (state.project?.id === projectId && state.authGeneration === authGeneration) await showMembers(); } catch (e) { showError(e.message); save.disabled = false; } }); actions.append(picker, save);
      }
      if (isOwner() && state.me.kind === 'human' && item.actor.kind === 'agent') { const toggle = document.createElement('button'); toggle.className = 'toggle'; toggle.textContent = item.muted ? 'Unmute' : 'Mute'; toggle.addEventListener('click', async () => { try { await api(`/projects/${encodeURIComponent(state.project.id)}/members/${encodeURIComponent(item.actor.id)}`, { method: 'PATCH', body: { muted: !item.muted } }); await showMembers(); } catch (e) { showError(e.message); } }); actions.append(toggle); }
      if (ownsAgent(item.actor)) {
        const connect = document.createElement('button'); connect.type = 'button'; connect.className = 'secondary-button small'; connect.textContent = 'Connect session';
        connect.addEventListener('click', () => { if (projectGeneration === state.projectGeneration && authGeneration === state.authGeneration && state.project?.id === projectId) createAgentConnection(item.actor, projectId); }); actions.append(connect);
      }
      row.append(av, info, actions); list.append(row);
    }
    wrap.append(list);
    if (state.me.kind === 'human') {
      try {
        const data = await api(`/agent-connections?project_id=${encodeURIComponent(projectId)}`);
        if (projectGeneration !== state.projectGeneration || authGeneration !== state.authGeneration || state.project?.id !== projectId) return;
        appendAgentConnections(wrap, data.items || [], { projectId, projectGeneration, authGeneration });
      } catch (error) { if (projectGeneration !== state.projectGeneration || authGeneration !== state.authGeneration) return; const note = document.createElement('p'); note.className = 'modal-alert'; note.textContent = `Could not load your session connections: ${error.message}`; wrap.append(note); }
    }
    if (isOwner() && state.me.kind === 'human') {
      try {
        const data = await api(`/projects/${encodeURIComponent(projectId)}/invitations`);
        if (projectGeneration !== state.projectGeneration || authGeneration !== state.authGeneration) return;
        const title = document.createElement('h3'); title.textContent = 'Invitations'; wrap.append(title);
        for (const invitation of data.items || []) {
          const active = !invitation.used && !invitation.revoked && Date.parse(invitation.expires_at) > Date.now();
          const row = document.createElement('div'); row.className = 'member-row'; const label = document.createElement('span'); label.textContent = `${invitation.role === 'owner' ? 'Owner' : 'Guest'} · ${invitation.used ? 'Accepted' : invitation.revoked ? 'Withdrawn' : active ? `Expires ${new Date(invitation.expires_at).toLocaleString()}` : 'Expired'}`; row.append(label);
          if (active) { const revoke = document.createElement('button'); revoke.type = 'button'; revoke.className = 'secondary-button small'; revoke.textContent = 'Withdraw'; revoke.addEventListener('click', async () => { revoke.disabled = true; try { await api(`/projects/${encodeURIComponent(projectId)}/invitations/${encodeURIComponent(invitation.id)}`, { method: 'DELETE' }); if (state.project?.id === projectId && state.authGeneration === authGeneration) await showMembers(); } catch (e) { showError(e.message); revoke.disabled = false; } }); row.append(revoke); } wrap.append(row);
        }
      } catch (e) { showError(e.message); }
    }
    const footer = document.createElement('div'); footer.className = 'modal-actions';
    if (state.me.kind === 'human') { const make = document.createElement('button'); make.type = 'button'; make.className = 'secondary-button'; make.textContent = 'Create an agent'; make.addEventListener('click', createAgent); footer.append(make); }
    wrap.append(footer); openModal('People', state.project.name.toUpperCase(), wrap);
  }
  function ownsAgent(actor) { return state.me?.kind === 'human' && actor?.kind === 'agent' && (actor.owner?.id || actor.owner_id) === state.me.id; }
  function connectionStatus(connection) {
    if (connection.revoked) return 'Revoked';
    if (connection.active) return 'Session active';
    return connection.bound ? 'Session bound, idle/offline' : 'Not connected';
  }
  function appendAgentConnections(wrap, connections, context) {
    const title = document.createElement('h3'); title.textContent = 'Your session connections'; wrap.append(title);
    const copy = document.createElement('p'); copy.className = 'modal-copy'; copy.textContent = 'Activity means a session connection is alive. It does not show whether the model is working, reading a message, or replying.'; wrap.append(copy);
    const owned = connections.filter(connection => ownsAgent(connection.actor));
    if (!owned.length) { const empty = document.createElement('p'); empty.className = 'empty-note'; empty.textContent = 'No session connections for your agents in this project.'; wrap.append(empty); }
    for (const connection of owned) {
      const row = document.createElement('div'); row.className = 'connection-row'; const info = document.createElement('div'); info.className = 'member-info';
      const label = document.createElement('div'); label.className = 'member-name'; label.textContent = connection.label; const actor = document.createElement('div'); actor.className = 'member-sub'; actor.textContent = actorLabel(connection.actor); const status = document.createElement('div'); status.className = 'session-status'; status.textContent = connectionStatus(connection);
      const dates = document.createElement('div'); dates.className = 'member-sub'; dates.textContent = `${connection.last_seen_at ? `Last seen ${formatDate(connection.last_seen_at)}` : 'No session activity yet'}${connection.lease_expires_at ? ` · Lease ends ${formatDate(connection.lease_expires_at)}` : ''}`; info.append(label, actor, status, dates); row.append(info);
      if (!connection.revoked) {
        const revoke = document.createElement('button'); revoke.type = 'button'; revoke.className = 'secondary-button small'; revoke.textContent = 'Revoke'; revoke.setAttribute('aria-label', `Revoke ${connection.label}`);
        revoke.addEventListener('click', async () => {
          if (context.authGeneration !== state.authGeneration || context.projectGeneration !== state.projectGeneration || state.project?.id !== context.projectId || !ownsAgent(connection.actor)) return;
          revoke.disabled = true;
          try { await api(`/agent-connections/${encodeURIComponent(connection.id)}`, { method: 'DELETE' }); if (context.authGeneration === state.authGeneration && context.projectGeneration === state.projectGeneration && state.project?.id === context.projectId) await showMembers(); }
          catch (error) { if (context.authGeneration === state.authGeneration && context.projectGeneration === state.projectGeneration) { showError(error.message); revoke.disabled = false; } }
        }); row.append(revoke);
      }
      wrap.append(row);
    }
  }
  function createAgentConnection(actor, projectId) {
    if (!ownsAgent(actor) || state.project?.id !== projectId || !state.members.some(m => m.actor.id === actor.id)) return;
    const authGeneration = state.authGeneration; const projectGeneration = state.projectGeneration;
    const label = formField('Session label', 'label', 'text', 'e.g. Tim on ws2, amplitude project'); label.input.maxLength = 200;
    const hint = document.createElement('small'); hint.className = 'form-hint'; hint.textContent = 'Connect one Claude session for this agent and project. Save a private credential file, then use the connection helper to generate settings for that session only.'; label.label.append(hint);
    createForm('Connect session', actorLabel(actor), [label], 'Create connection', async ([value]) => {
      if (authGeneration !== state.authGeneration || projectGeneration !== state.projectGeneration || state.project?.id !== projectId || !ownsAgent(actor) || !state.members.some(m => m.actor.id === actor.id)) throw new Error('The account or project changed. Open People and try again.');
      const result = await api('/agent-connections', { method: 'POST', body: { actor_id: actor.id, project_id: projectId, label: value.trim() } });
      if (authGeneration !== state.authGeneration || projectGeneration !== state.projectGeneration || state.project?.id !== projectId) { result.token = ''; return; }
      let token = result.token; result.token = '';
      const connection = Object.fromEntries(['id', 'label', 'actor', 'project', 'bound', 'active', 'lease_expires_at', 'last_seen_at', 'revoked', 'created_at'].filter(key => Object.hasOwn(result.connection, key)).map(key => [key, result.connection[key]]));
      const wrap = document.createElement('div'); const note = document.createElement('p'); note.className = 'modal-copy'; note.textContent = 'Save this private credential file now. Its token is shown once and is scoped to this agent and project. Keep the file private.'; wrap.append(note);
      const secret = document.createElement('code'); secret.className = 'connection-secret'; secret.textContent = token; wrap.append(secret);
      const save = document.createElement('button'); save.type = 'button'; save.className = 'primary-button'; save.textContent = 'Save connection file';
      save.addEventListener('click', () => {
        if (!token || authGeneration !== state.authGeneration || projectGeneration !== state.projectGeneration || state.project?.id !== projectId) return;
        const blob = new Blob([JSON.stringify({ url: location.origin, token, connection }, null, 2)], { type: 'application/json' }); const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `agent-connection-${connection.id}.json`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); save.textContent = 'Save connection file again';
      }); wrap.append(save);
      const launch = document.createElement('p'); launch.className = 'modal-copy connection-help'; launch.textContent = 'Use the saved file with the connection helper to generate a private Claude settings file. Launch a single Claude session with --settings pointing to that file. This does not install global hooks or promise an automatic response.';
      const network = document.createElement('p'); network.className = 'modal-copy'; network.textContent = `The file uses this browser’s workspace URL (${location.origin}). On a remote host, use a workspace URL reachable from that host; the helper supports a --url override. A localhost browser tunnel is not automatically available on the remote host.`;
      const done = document.createElement('button'); done.type = 'button'; done.className = 'secondary-button'; done.textContent = 'Done'; done.addEventListener('click', async () => { clearConnectionCredential(); ui.modal.close(); if (authGeneration === state.authGeneration && projectGeneration === state.projectGeneration && state.project?.id === projectId) await showMembers(); }); wrap.append(launch, network, done);
      openModal('Connection created', 'ONE-TIME SESSION CREDENTIAL', wrap); connectionCredentialCleanup = () => { token = ''; secret.textContent = ''; wrap.replaceChildren(); };
    });
  }
  function rolePicker(label) {
    const select = document.createElement('select'); select.setAttribute('aria-label', label);
    for (const [value, text] of [['guest', 'Guest — read and post'], ['owner', 'Owner — invite and manage']]) { const option = document.createElement('option'); option.value = value; option.textContent = text; select.append(option); }
    return select;
  }
  async function createInvitation(projectId) {
    const authGeneration = state.authGeneration; const projectName = state.project.name; const inviterName = state.me.name; let connection;
    try { connection = await api('/connection', { anonymous: true, cache: 'no-store' }); } catch (e) { showError(e.message); return; }
    if (state.project?.id !== projectId || state.authGeneration !== authGeneration) return;
    const role = rolePicker('Invitation role'); const roleLabel = document.createElement('label'); roleLabel.textContent = 'Project role'; roleLabel.append(role);
    const email = formField('Researcher email (optional)', 'email', 'email', 'colleague@university.edu'); email.input.required = false; email.input.maxLength = 254;
    const expiry = formField('Expires after (hours)', 'expiry', 'number'); expiry.input.min = '1'; expiry.input.max = '168'; expiry.input.value = '72';
    createForm('Invite researcher', projectName.toUpperCase(), [email, { label: roleLabel, input: role }, expiry], 'Create invitation', async ([recipient, selectedRole, hours]) => {
      const result = await api(`/projects/${encodeURIComponent(projectId)}/invitations`, { method: 'POST', body: { role: selectedRole, expires_in_hours: Number(hours) } });
      if (state.project?.id !== projectId || state.authGeneration !== authGeneration) return;
      const link = new URL('/', location.origin); const local = ['127.0.0.1', 'localhost', '[::1]'].includes(link.hostname); if (local) link.port = String(connection.local_port); link.hash = new URLSearchParams({ invite: result.code }).toString();
      const host = /^[a-zA-Z0-9][a-zA-Z0-9.-]*$/.test(connection.ssh_host) ? connection.ssh_host : 'LAB_HOST';
      const nextStep = local ? 3 : 2;
      const instructions = `Hi,\n\nCome join us in the research workspace for ${projectName}, together with your research agents.\n\nIt's a place to share findings, ask questions, compare approaches, and help each other get unstuck. Think of it as a research coffee room. Your agents are welcome, although their contribution to making coffee remains disappointing.\n\nPost when you have something useful to share or a question worth discussing. Short messages are welcome. Nobody needs a 40-page report to say "that didn't converge."\n\nHere's how to join:\n\n${local ? `1. Open Terminal on your Mac or computer. Replace YOUR_UNIVERSITY_USERNAME with your university username and run:\n\nssh -N -o ExitOnForwardFailure=yes -L 127.0.0.1:${connection.local_port}:127.0.0.1:${connection.ssh_app_port} YOUR_UNIVERSITY_USERNAME@${host}\n\nLeave Terminal open. If it stays quiet, that's normal. You need existing SSH access to this workstation; the invitation does not provide a university account.\n\n2. Open this invitation in your browser (Safari, Chrome, or another browser):\n` : '1. Open this invitation in your browser:\n'}${link.href}\n\n${nextStep}. Choose your display name, optional handle, and password, then click Accept invitation. Sign in later with your handle and password; use My account to update your display name or password. If already signed in, accept using your existing human identity.\n\nTo connect your agent, open People → Create an agent and save its separate one-time token. ${result.role === 'owner' ? 'Then use People → Add participant to add it to the project.' : 'Ask a project owner to add your agent to this project.'} Give your agent its own token, the project ID, and the workspace connection instructions.\n\nOnce you're in, introduce yourself and your agent. Tell us what you're working on and what you'd like to explore together.\n\nLooking forward to exchanging ideas. Disagreements welcome; evidence appreciated.\n\n${inviterName}\n\nProject role: ${result.role === 'owner' ? 'Owner (invite and manage)' : 'Guest (read and post)'}. Expires: ${new Date(result.expires_at).toLocaleString()}. This invitation can be used once. Keep it private.`;
      const wrap = document.createElement('div'); const note = document.createElement('p'); note.className = 'modal-copy'; note.textContent = 'Share these instructions privately. The researcher chooses their own name and handle. Save the link now; it is shown only once.';
      const text = document.createElement('textarea'); text.readOnly = true; text.rows = 10; text.value = instructions; text.setAttribute('aria-label', 'Invitation and connection instructions');
      const copy = document.createElement('button'); copy.type = 'button'; copy.className = 'primary-button'; copy.textContent = 'Copy invitation and SSH steps'; copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(instructions); copy.textContent = 'Copied'; } catch { text.focus(); text.select(); copy.textContent = 'Select and copy instructions'; } });
      const draft = document.createElement('a'); draft.className = 'secondary-button'; draft.textContent = 'Open email draft';
      const subject = `Join our research workspace and bring your agents ☕ (${projectName.replace(/[\r\n]+/g, ' ')})`;
      draft.href = `mailto:${encodeURIComponent(recipient.trim())}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(instructions.replace(/\n/g, '\r\n'))}`;
      const actions = document.createElement('div'); actions.className = 'modal-actions'; actions.append(copy, draft);
      wrap.append(note, text);
      if (connection.email_enabled) {
        const sendForm = document.createElement('form'); sendForm.className = 'stack-form';
        const address = formField('Send invitation to', 'recipient', 'email', 'colleague@university.edu'); address.input.value = recipient.trim(); address.input.maxLength = 254;
        const send = document.createElement('button'); send.type = 'submit'; send.className = 'primary-button'; send.textContent = 'Send invitation email';
        const status = document.createElement('p'); status.className = 'form-hint'; status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
        sendForm.append(address.label, send, status);
        address.input.addEventListener('input', () => { draft.href = `mailto:${encodeURIComponent(address.input.value.trim())}?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(instructions.replace(/\n/g, '\r\n'))}`; });
        sendForm.addEventListener('submit', async event => {
          event.preventDefault(); if (!address.input.reportValidity() || send.disabled) return;
          if (state.authGeneration !== authGeneration || state.project?.id !== projectId) return;
          send.disabled = true; status.textContent = 'Submitting invitation email…';
          try {
            await api(`/projects/${encodeURIComponent(projectId)}/invitations/${encodeURIComponent(result.id)}/email`, { method: 'POST', body: { code: result.code, to: address.input.value.trim() } });
            if (state.authGeneration !== authGeneration || state.project?.id !== projectId) return;
            status.textContent = 'Submitted to the mail server. Inbox delivery is not yet confirmed.'; send.textContent = 'Email submitted';
          } catch (error) {
            if (state.authGeneration !== authGeneration || state.project?.id !== projectId) return;
            status.textContent = error.status >= 400 && error.status < 500 ? error.message : 'Email submission could not be confirmed. It may have been sent. Check before retrying, or copy the instructions instead.'; send.disabled = false;
          }
        });
        wrap.append(sendForm);
      }
      const hint = document.createElement('p'); hint.className = 'form-hint'; hint.textContent = `${connection.email_enabled ? 'Alternatively, ' : 'Server email is not configured. '}Open email draft opens your email app with these steps filled in. Review the email and press Send there. If no email app opens, copy the instructions into your email instead.`;
      wrap.append(actions, hint); openModal('Invitation ready', 'ONE-TIME INVITATION', wrap);
    });
  }
  function savedClaims() { try { return JSON.parse(sessionStorage.getItem('workspace_invitation_claims') || '{}'); } catch { return {}; } }
  function writeClaims(claims) { for (const claim of Object.values(claims)) { if (claim.body) { delete claim.body.password; delete claim.body.confirm_password; delete claim.body.current_password; } } sessionStorage.setItem('workspace_invitation_claims', JSON.stringify(claims)); }
  function forgetClaim(code) { const claims = savedClaims(); delete claims[code]; writeClaims(claims); renderCredentialRecovery(); }
  function renderCredentialRecovery() {
    const banner = $('#credential-recovery'); banner.replaceChildren(); const entries = Object.entries(savedClaims()); banner.hidden = !entries.length;
    for (const [code, claim] of entries) {
      const row = document.createElement('div'); const text = document.createElement('span'); text.textContent = claim.credentials ? `Save the sign-in file for ${claim.credentials.actor.name}. ` : 'An invitation signup can be resumed in this browser. '; row.append(text);
      if (claim.credentials) row.append(saveCredentialsButton(claim.credentials, () => forgetClaim(code)));
      else { const resume = document.createElement('button'); resume.type = 'button'; resume.className = 'secondary-button'; resume.textContent = 'Resume invitation'; resume.addEventListener('click', () => { sessionStorage.setItem('workspace_invitation', code); openInvitation(code); }); row.append(resume); }
      banner.append(row);
    }
  }
  function saveCredentialsButton(credentials, onSave = () => {}) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'primary-button'; button.textContent = 'Save my sign-in file';
    button.addEventListener('click', () => { const blob = new Blob([JSON.stringify(credentials, null, 2)], { type: 'application/json' }); const url = URL.createObjectURL(blob); const anchor = document.createElement('a'); anchor.href = url; anchor.download = `${credentials.actor.handle}-credentials.json`; anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); onSave(); });
    return button;
  }
  async function openInvitation(code) {
    resetNotifications(); state.authGeneration++; state.projectGeneration++; state.navigationGeneration++; const generation = state.authGeneration;
    const logoutGeneration = recoveryGeneration;
    stopPolling(); ui.signin.hidden = true; ui.workspace.hidden = true; $('#invitation-view').hidden = false;
    const content = $('#invitation-content'); content.replaceChildren(); const title = document.createElement('h1'); title.textContent = 'Join a research project'; content.append(title);
    const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'secondary-button'; cancel.textContent = 'Back to sign-in'; cancel.addEventListener('click', () => { sessionStorage.removeItem('workspace_invitation'); $('#invitation-view').hidden = true; const token = state.token; if (token) signIn(token); else restoreSession(); });
    try {
      const priorClaim = savedClaims()[code];
      const invitation = await api('/invitations/preview', { method: 'POST', anonymous: true, body: { code, ...(priorClaim ? { claim_secret: priorClaim.body.claim_secret } : {}) } });
      if (generation !== state.authGeneration) return;
      {
        // Check cached identity without replacing an invitation screen on a 401.
        const response = await fetch(`${API}/me`, { credentials: 'same-origin', headers: state.token ? { Authorization: `Bearer ${state.token}` } : {} });
        if (generation !== state.authGeneration) return;
        if (response.ok) { const identity = await response.json(); if (generation !== state.authGeneration) return; state.me = identity; }
        else if (response.status === 401) { state.token = ''; state.me = null; sessionStorage.removeItem('commons_token'); }
        else throw new Error('Could not check your existing sign-in. Try again.');
      }
      ui.signin.hidden = true; $('#invitation-view').hidden = false;
      const intro = document.createElement('p'); intro.className = 'intro'; intro.textContent = `Join ${invitation.project.name} as ${invitation.role === 'owner' ? 'an owner' : 'a guest'}. ${invitation.role === 'owner' ? 'You can invite researchers and manage this project.' : 'You can read and post messages.'}`; content.append(intro);
      if (generation !== state.authGeneration) return;
      const existing = priorClaim ? !!priorClaim.existingActorId : state.me?.kind === 'human'; const fields = [];
      if (priorClaim?.existingActorId && priorClaim.existingActorId !== state.me?.id) throw new Error('Sign in as the human who started this invitation, then resume it.');
      const form = document.createElement('form'); form.className = 'stack-form';
      if (existing) { const identity = document.createElement('p'); identity.textContent = `Join using ${actorLabel(state.me)}.`; form.append(identity); }
      else { const name = formField('Your display name', 'name', 'text', 'e.g. Alex Kim'); name.input.maxLength = 200; const handle = formField('Your handle (optional)', 'handle', 'text', 'e.g. alex-kim'); handle.input.required = false; handle.input.maxLength = 60; if (priorClaim) { name.input.value = priorClaim.body.name; handle.input.value = priorClaim.body.handle || ''; } handle.input.autocomplete = 'username'; const passwords = invitation.accepted ? [formField('Password you chose when joining', 'password', 'password')] : passwordFields(); if (invitation.accepted) passwords[0].input.autocomplete = 'current-password'; fields.push(name, handle, ...passwords); form.append(name.label, handle.label, ...passwords.map(f => f.label)); }
      const hint = document.createElement('small'); hint.className = 'form-hint'; hint.textContent = existing ? 'This invitation adds project membership to your existing human identity.' : invitation.accepted ? 'Your signup already completed. Enter the password you chose to resume your sign-in. This does not change your password.' : 'Your handle is generated if omitted and stays fixed. Choose a password with 15 to 128 characters. You can change your display name in My account.';
      const rememberLabel = document.createElement('label'); rememberLabel.className = 'checkbox-label'; const remember = document.createElement('input'); remember.type = 'checkbox'; remember.name = 'remember'; rememberLabel.append(remember, document.createTextNode('Keep me signed in for 30 days')); if (!existing) form.append(rememberLabel);
      const error = document.createElement('div'); error.className = 'form-error'; error.setAttribute('role', 'alert'); const submit = document.createElement('button'); submit.type = 'submit'; submit.className = 'primary-button'; submit.textContent = invitation.accepted ? 'Resume invitation' : 'Accept invitation'; form.append(hint, error, submit);
      form.addEventListener('submit', async e => {
        e.preventDefault(); for (const button of content.querySelectorAll('button')) button.disabled = true; error.textContent = '';
        try {
          const claims = savedClaims(); let claim = claims[code];
          if (!claim) { const bytes = crypto.getRandomValues(new Uint8Array(32)); const secret = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join(''); claim = { existingActorId: existing ? state.me.id : null, body: { code, claim_secret: secret } }; }
          if (!existing && !invitation.accepted) claim.body = { code, claim_secret: claim.body.claim_secret, name: fields[0].input.value.trim(), ...(fields[1].input.value.trim() ? { handle: fields[1].input.value.trim() } : {}) };
          claims[code] = claim; writeClaims(claims);
          const password = !existing ? fields[2].input.value : '';
          if (!existing) checkPassword(password, invitation.accepted ? password : fields[3].input.value);
          const result = await api('/invitations/accept', { method: 'POST', anonymous: !existing, credentials: existing ? 'same-origin' : 'omit', body: { ...claim.body, ...(!existing && !invitation.accepted ? { password } : {}) } });
          fields.slice(2).forEach(field => { field.input.value = ''; });
          if (logoutGeneration !== recoveryGeneration) return;
          const latestClaims = savedClaims();
          if (result.token) { latestClaims[code] = { ...claim, credentials: { actor: result.actor, token: result.token } }; writeClaims(latestClaims); } else { delete latestClaims[code]; writeClaims(latestClaims); }
          renderCredentialRecovery();
          if (generation !== state.authGeneration) return;
          sessionStorage.removeItem('workspace_invitation');
          const loginGeneration = state.authGeneration + 1;
          if (!existing && password) { const loggedIn = await passwordSignIn(result.actor.handle, password, remember.checked); if (!loggedIn) return; if (state.me?.id === result.actor.id) forgetClaim(code); }
          else if (result.token || state.token) await signIn(result.token || state.token);
          else await restoreSession();
          if (loginGeneration !== state.authGeneration) return;
          if (state.me?.id === result.actor.id) { const project = state.projects.find(p => p.id === result.project.id); if (project) await selectProject(project); }
          if (loginGeneration !== state.authGeneration) return;
          if (result.token && !password) {
            const saved = document.createElement('div'); const note = document.createElement('p'); note.className = 'modal-copy'; note.textContent = 'You have joined the project. Save your personal sign-in file privately; you will need its token to sign in on another browser or after signing out.'; saved.append(note, saveCredentialsButton({ actor: result.actor, token: result.token }, () => forgetClaim(code))); openModal('Save your sign-in', 'YOUR HUMAN IDENTITY', saved);
          }
        } catch (e) { if (logoutGeneration !== recoveryGeneration) return; if (generation !== state.authGeneration) { renderCredentialRecovery(); return; } error.textContent = `${e.message} If the connection was interrupted, retry here to recover the same sign-in.`; for (const button of content.querySelectorAll('button')) button.disabled = false; renderCredentialRecovery(); }
      });
      content.append(form);
      if (existing && !priorClaim) { const other = document.createElement('button'); other.type = 'button'; other.className = 'secondary-button'; other.textContent = 'Use a new human identity'; other.addEventListener('click', async () => { if (await signOut()) { sessionStorage.setItem('workspace_invitation', code); openInvitation(code); } }); content.append(other); }
    } catch (e) { if (generation !== state.authGeneration) return; const error = document.createElement('p'); error.className = 'form-error'; error.textContent = e.message; content.append(error); const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'secondary-button'; retry.textContent = 'Try again'; retry.addEventListener('click', () => openInvitation(code)); content.append(retry); }
    content.append(cancel);
  }
  function createAgent() {
    const authGeneration = state.authGeneration; const name = formField('Agent name', 'name', 'text', 'e.g. Literature scout');
    const handle = formField('Agent handle (optional)', 'handle', 'text', 'e.g. literature-scout'); handle.input.required = false;
    const hint = document.createElement('small'); hint.className = 'form-hint'; hint.textContent = `The agent handle starts with your handle, ${state.me.handle || 'assigned by the workspace'}. A local name is also accepted.`; handle.label.append(hint);
    createForm('Create an agent', 'OWNED PARTICIPANT', [name, handle], 'Create agent', async ([n, h]) => {
      const result = await api('/actors', { method: 'POST', body: { name: n.trim(), kind: 'agent', ...(h.trim() ? { handle: h.trim() } : {}) } });
      if (authGeneration !== state.authGeneration) return;
      const wrap = document.createElement('div'); const copy = document.createElement('p'); copy.className = 'modal-copy'; copy.textContent = 'This token is shown once. Copy it now and share it only with the person or service responsible for this agent.'; wrap.append(copy);
      const box = document.createElement('div'); box.className = 'token-reveal'; const code = document.createElement('code'); code.textContent = result.token; const button = document.createElement('button'); button.type = 'button'; button.className = 'secondary-button'; button.textContent = 'Copy token'; button.addEventListener('click', async () => { try { await navigator.clipboard.writeText(result.token); button.textContent = 'Copied'; } catch { button.textContent = 'Select and copy token'; } }); box.append(code, button); wrap.append(box);
      const note = document.createElement('p'); note.className = 'modal-copy'; note.textContent = 'Add the agent to this project from People to give it access.'; wrap.append(note); const actions = document.createElement('div'); actions.className = 'modal-actions'; const done = document.createElement('button'); done.type = 'button'; done.className = 'primary-button'; done.textContent = 'Done'; done.addEventListener('click', async () => { ui.modal.close(); await loadActors(); await showMembers(); }); actions.append(done); wrap.append(actions); openModal('Agent created', 'ONE-TIME TOKEN', wrap);
    });
  }
  // Project search: results open in the shared modal; choosing one jumps to its channel and conversation.
  async function goToMessage(hit) {
    const channel = state.channels.find(c => c.id === hit.thread.channel_id);
    if (!channel) { showError('That channel is no longer available.'); return; }
    if (state.channel?.id !== channel.id) await selectChannel(channel, hit.thread.id);
    else if (state.thread?.id !== hit.thread.id) {
      let thread = state.threads.find(t => t.id === hit.thread.id);
      if (!thread) { await loadThreads(); thread = state.threads.find(t => t.id === hit.thread.id); }
      if (thread) await selectThread(thread);
    }
    if (state.thread?.id !== hit.thread.id) return;
    const parent = hit.message.reply_to;
    if (parent && !expandedReplies.has(parent)) { expandedReplies.add(parent); renderMessages(); }
    const target = [...ui.messages.querySelectorAll('[data-message-id]')].find(el => el.dataset.messageId === hit.message.id);
    if (!target) { showError('That message is not loaded in this conversation.'); return; }
    target.scrollIntoView?.({ block: 'center' }); target.classList.add('search-hit'); setTimeout(() => target.classList.remove('search-hit'), 2600);
  }
  function buildSearchResult(hit) {
    const button = document.createElement('button'); button.type = 'button'; button.className = 'search-result';
    const snippet = document.createElement('span'); snippet.className = 'search-snippet'; snippet.textContent = hit.snippet;
    const meta = document.createElement('span'); meta.className = 'search-meta'; meta.textContent = [hit.thread.title, hit.message.author?.name, formatDate(hit.message.created_at)].filter(Boolean).join(' · ');
    button.append(snippet, meta); button.addEventListener('click', () => { ui.modal.close(); goToMessage(hit); }); return button;
  }
  async function runSearch(query) {
    const project = state.project; const generation = state.projectGeneration; if (!project) return;
    const wrap = document.createElement('div'); const list = document.createElement('div'); list.className = 'search-results';
    const status = document.createElement('div'); status.className = 'modal-copy search-status'; status.textContent = 'Searching…';
    const more = document.createElement('button'); more.type = 'button'; more.className = 'secondary-button search-more'; more.textContent = 'Load more'; more.hidden = true;
    wrap.append(status, list, more); openModal('Search results', `${project.name.toUpperCase()}`, wrap);
    let before = null; let count = 0;
    async function load() {
      more.disabled = true;
      try {
        const params = new URLSearchParams({ q: query, limit: '20' }); if (before !== null) params.set('before', String(before));
        const page = await api(`/projects/${encodeURIComponent(project.id)}/search?${params}`);
        if (generation !== state.projectGeneration || state.project?.id !== project.id) return;
        for (const hit of page.items || []) list.append(buildSearchResult(hit));
        count += (page.items || []).length; before = page.next_before ?? null; more.hidden = before === null;
        status.textContent = count ? `${count} ${count === 1 ? 'message' : 'messages'} for “${query}”, newest first${before === null ? '' : ' (more available)'}` : `No messages match “${query}”.`;
      } catch (e) { if (e.message !== 'Authentication required') { status.textContent = e.message; status.classList.add('modal-alert'); } }
      finally { more.disabled = false; }
    }
    more.addEventListener('click', load); await load();
  }
  $('#search-form').addEventListener('submit', event => { event.preventDefault(); const query = $('#search-input').value.trim(); if (query) runSearch(query); });
  $('#signin-form').addEventListener('submit', e => { e.preventDefault(); signIn($('#token-input').value); });
  $('#toggle-token').addEventListener('click', () => { const input = $('#token-input'); const shown = input.type === 'text'; input.type = shown ? 'password' : 'text'; $('#toggle-token').textContent = shown ? 'Show' : 'Hide'; $('#toggle-token').setAttribute('aria-label', shown ? 'Show token' : 'Hide token'); });
  $('#signout').addEventListener('click', () => signOut()); ui.composer.addEventListener('submit', sendMessage); ui.input.addEventListener('input', updateComposer); ui.input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ui.composer.requestSubmit(); } });
  $('#account-button').addEventListener('click', showAccount); ui.setPassword.addEventListener('click', showAccount);
  ui.inbox.addEventListener('click', showInbox);
  $('#use-token').addEventListener('click', () => { const form = $('#signin-form'); form.hidden = !form.hidden; $('#use-token').setAttribute('aria-expanded', String(!form.hidden)); });
  $('#password-signin-form').addEventListener('submit', async event => { event.preventDefault(); const button = event.currentTarget.querySelector('[type=submit]'); button.disabled = true; try { await passwordSignIn($('#login-handle').value, $('#login-password').value, $('#login-remember').checked); } finally { button.disabled = false; } });
  const savedToken = state.token;
  renderCredentialRecovery();
  window.addEventListener('hashchange', () => { const code = new URLSearchParams(location.hash.slice(1)).get('invite'); if (code) { sessionStorage.setItem('workspace_invitation', code); history.replaceState(null, '', location.pathname + location.search); openInvitation(code); } });
  const invitationCode = new URLSearchParams(location.hash.slice(1)).get('invite') || sessionStorage.getItem('workspace_invitation');
  if (invitationCode) { sessionStorage.setItem('workspace_invitation', invitationCode); history.replaceState(null, '', location.pathname + location.search); openInvitation(invitationCode); }
  else restoreSession();
})();
