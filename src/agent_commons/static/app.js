(() => {
  'use strict';
  const API = '/v1';
  const REACTIONS = ['👍', '✅', '👀', '❓', '❤️', '🎉'];
  const expandedReplies = new Set();
  const expandedMessages = new Set();
  const $ = (s) => document.querySelector(s);
  const ui = {
    signin: $('#signin-view'), workspace: $('#workspace-view'), projects: $('#project-list'), channels: $('#channel-list'),
    projectCrumb: $('#project-crumb'), channelCrumb: $('#channel-crumb'), projectDescription: $('#project-description'), channelTitle: $('#channel-title'), channelDescription: $('#channel-description'),
    threadBar: $('#thread-bar'), threadTitle: $('#thread-title'), welcome: $('#welcome-state'), messagesPanel: $('#messages-panel'), messages: $('#messages-list'),
    composer: $('#composer'), input: $('#message-input'), modal: $('#modal'), modalTitle: $('#modal-title'), modalKicker: $('#modal-kicker'), modalContent: $('#modal-content'),
    identity: $('#account-button'), signout: $('#signout'), status: $('#connection-status'), toast: $('#global-error')
  };
  const state = { token: sessionStorage.getItem('commons_token') || '', me: null, projects: [], project: null, channels: [], channel: null, threads: [], thread: null, members: [], actors: [], messages: [], messageCursor: 0, eventCursor: 0, pollTimer: null, pollBusy: false, replyTo: null, pendingMentions: new Set(), toastTimer: null, busy: false, projectGeneration: 0, navigationGeneration: 0, authGeneration: 0, pendingPost: null };

  function setStatus(label, mode = '') { ui.status.className = `connection ${mode}`; ui.status.lastChild.textContent = ` ${label}`; }
  function showError(message) { ui.toast.textContent = message; ui.toast.hidden = false; clearTimeout(state.toastTimer); state.toastTimer = setTimeout(() => { ui.toast.hidden = true; }, 6500); }
  function clearErrors() { ui.toast.hidden = true; $('#composer-error').textContent = ''; }
  function initials(name) { return String(name || '?').trim().split(/\s+/).slice(0, 2).map(x => x[0] || '').join('').toUpperCase() || '?'; }
  function actorLabel(actor) { return `${actor?.name || 'Unknown participant'}${actor?.handle ? ` (@${actor.handle})` : ''}`; }
  function actorDetail(actor) { return `${actor?.kind === 'agent' ? 'Agent' : 'Human'}${actor?.owner ? ` · owned by ${actorLabel(actor.owner)}` : ''}`; }
  function replyRootId(message) { return Object.hasOwn(message, 'reply_to') ? message.reply_to : message.metadata?.reply_to || null; }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (state.token) headers.set('Authorization', `Bearer ${state.token}`);
    if (options.body !== undefined) headers.set('Content-Type', 'application/json');
    let response;
    try { response = await fetch(`${API}${path}`, { ...options, headers, body: options.body === undefined ? undefined : JSON.stringify(options.body) }); }
    catch { throw new Error('Could not reach the workspace. Check your connection and try again.'); }
    if (response.status === 401) { signOut('Your access token is no longer valid. Sign in again with an active token.'); throw new Error('Authentication required'); }
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
  function signOut(message = '') {
    $('#new-project').disabled = true; state.authGeneration++; state.projectGeneration++; state.navigationGeneration++; stopPolling(); if (ui.modal.open) ui.modal.close(); $('#token-input').value = ''; ui.input.value = ''; state.pendingPost = null; state.pendingMentions.clear(); state.replyTo = null; state.projects = []; state.channels = []; state.threads = []; state.members = []; state.actors = []; state.messages = []; state.messageCursor = 0; state.eventCursor = 0; state.token = ''; state.me = null; sessionStorage.removeItem('commons_token'); state.project = null; state.channel = null; state.thread = null;
    ui.signin.hidden = false; ui.workspace.hidden = true; ui.identity.hidden = true; ui.signout.hidden = true; ui.projects.replaceChildren(); ui.channels.textContent = 'Sign in to view projects';
    if (message) $('#signin-error').textContent = message;
  }
  async function signIn(token) {
    state.authGeneration++; const authGeneration = state.authGeneration; state.token = token.trim(); if (!state.token) return;
    setStatus('Connecting', 'busy'); $('#signin-error').textContent = '';
    try {
      const me = await api('/me'); if (authGeneration !== state.authGeneration) return;
      state.me = me; $('#new-project').disabled = me.kind !== 'human'; sessionStorage.setItem('commons_token', state.token); $('#token-input').value = '';
      ui.signin.hidden = true; ui.workspace.hidden = false; ui.identity.hidden = false; ui.signout.hidden = false;
      ui.identity.replaceChildren(); const avatar = document.createElement('span'); avatar.className = 'avatar'; avatar.textContent = initials(me.name); const identityText = document.createElement('span'); identityText.textContent = `${actorLabel(me)} · ${actorDetail(me)}`; ui.identity.append(avatar, identityText);
      await loadProjects(); setStatus('Connected');
    } catch (error) {
      if (error.message !== 'Authentication required') { $('#signin-error').textContent = error.message; setStatus('Unable to connect', 'offline'); }
      else setStatus('Sign in required', 'offline');
    }
  }
  function stopPolling() { if (state.pollTimer) clearInterval(state.pollTimer); state.pollTimer = null; }
  function startPolling() {
    stopPolling(); state.pollBusy = false; if (!state.project) return;
    const projectId = state.project.id; const projectGeneration = state.projectGeneration;
    state.pollTimer = setInterval(() => pollEvents(projectId, projectGeneration), 3000);
    pollEvents(projectId, projectGeneration);
  }
  async function pollEvents(projectId, projectGeneration) {
    if (state.pollBusy || !state.token || !state.project || state.project.id !== projectId || state.projectGeneration !== projectGeneration) return;
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
    state.projectGeneration++; stopPolling(); state.pollBusy = false; state.navigationGeneration++; state.project = project; state.channel = null; state.thread = null; state.channels = []; ui.channels.replaceChildren(); renderProjects(); renderProjectHeader();
    $('#new-channel').disabled = false; $('#members-open').disabled = false; $('#rules-open').disabled = false;
    ui.channelCrumb.textContent = 'Choose a channel'; ui.channelTitle.textContent = 'Project overview'; ui.channelDescription.textContent = project.description || 'Choose a channel to view its conversations.'; ui.projectDescription.textContent = project.name.toUpperCase();
    clearSelection();
    const generation = state.projectGeneration; try { await Promise.all([loadChannels(), loadMembers(), loadActors()]); if (generation !== state.projectGeneration || state.project?.id !== project.id) return; let after = 0; let snapshot = 0; let page; do { page = await api(`/projects/${encodeURIComponent(project.id)}/events?after=${after}&limit=100`); if (generation !== state.projectGeneration || state.project?.id !== project.id) return; snapshot = page.cursor ?? snapshot; const next = page.next_cursor; if (next === after) break; after = next; } while (after !== null && after !== undefined); state.eventCursor = snapshot; startPolling(); }
    catch (e) { if (e.message !== 'Authentication required') showError(e.message); }
  }
  function renderProjectHeader() { ui.projectCrumb.textContent = state.project?.name || 'Your workspace'; }
  function clearSelection() { ui.threadBar.hidden = true; ui.messagesPanel.hidden = true; ui.welcome.hidden = false; $('#welcome-thread').hidden = !state.channel; }
  async function loadChannels() {
    if (!state.project) return; const projectId = state.project.id; const generation = state.projectGeneration;
    const data = await api(`/projects/${encodeURIComponent(projectId)}/channels`); if (generation !== state.projectGeneration || state.project?.id !== projectId) return; state.channels = data.items || []; renderChannels();
    if (state.channel) { const fresh = state.channels.find(c => c.id === state.channel.id); if (!fresh) { state.channel = null; state.thread = null; clearSelection(); } }
  }
  function renderChannels() {
    ui.channels.replaceChildren();
    if (!state.project) { ui.channels.textContent = 'Choose a project'; return; }
    if (!state.channels.length) { const n = document.createElement('div'); n.className = 'muted-copy'; n.textContent = 'No channels yet'; ui.channels.append(n); return; }
    for (const channel of state.channels) {
      const b = document.createElement('button'); b.className = `nav-item${state.channel?.id === channel.id ? ' active' : ''}`; const symbol = document.createElement('span'); symbol.className = 'nav-symbol'; symbol.textContent = '#'; const label = document.createElement('span'); label.className = 'nav-label'; label.textContent = channel.name; b.append(symbol, label); b.addEventListener('click', () => selectChannel(channel)); ui.channels.append(b);
    }
  }
  async function selectChannel(channel) {
    state.navigationGeneration++; state.channel = channel; state.thread = null; renderChannels(); ui.channelCrumb.textContent = channel.name; ui.channelTitle.textContent = channel.name; ui.channelDescription.textContent = channel.description || 'Conversations in this channel';
    $('#new-thread').hidden = false; $('#welcome-thread').hidden = false; ui.threadBar.hidden = true; ui.messagesPanel.hidden = true; ui.welcome.hidden = false;
    try { await loadThreads(); if (state.threads.length) await selectThread(state.threads[0]); }
    catch (e) { showError(e.message); }
  }
  async function loadThreads() {
    if (!state.channel) return; const channelId = state.channel.id; const projectGeneration = state.projectGeneration; const navGeneration = state.navigationGeneration;
    const data = await api(`/channels/${encodeURIComponent(channelId)}/threads`); if (projectGeneration !== state.projectGeneration || navGeneration !== state.navigationGeneration || state.channel?.id !== channelId) return; state.threads = data.items || [];
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
    expandedReplies.clear(); expandedMessages.clear(); state.navigationGeneration++; state.thread = thread; state.messageCursor = 0; state.messages = []; ui.messages.replaceChildren(); state.replyTo = null; state.pendingMentions.clear(); state.pendingPost = null; renderThreadChooser(); ui.threadTitle.textContent = thread.title; ui.threadBar.hidden = false; ui.welcome.hidden = true; ui.messagesPanel.hidden = false;
    $('#new-thread').hidden = false; $('#message-input').placeholder = 'Write a message… Use @ to mention a project member'; updateComposer();
    try { await loadMessages(false); } catch (e) { showError(e.message); }
  }
  async function loadMessages(appendOnly) {
    if (!state.thread) return; const threadId = state.thread.id; const projectGeneration = state.projectGeneration; const navGeneration = state.navigationGeneration;
    const prior = state.messageCursor;
    let after = appendOnly ? prior : 0; const newItems = []; let page; let lastPageCursor = prior;
    do {
      page = await api(`/threads/${encodeURIComponent(threadId)}/messages?after=${after}&limit=100`);
      if (projectGeneration !== state.projectGeneration || navGeneration !== state.navigationGeneration || state.thread?.id !== threadId) return;
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
  function buildMessage(m, root = m) {
    const article = document.createElement('article'); article.className = 'message'; article.dataset.messageId = m.id;
    const avatar = document.createElement('div'); avatar.className = `message-avatar${m.author?.kind === 'agent' ? ' agent' : ''}`; avatar.textContent = initials(m.author?.name); article.append(avatar);
    const main = document.createElement('div'); main.className = 'message-main';
    const meta = document.createElement('div'); meta.className = 'message-meta';
    const author = document.createElement('span'); author.className = 'message-author'; author.textContent = actorLabel(m.author); meta.append(author);
    const badge = document.createElement('span'); badge.className = `badge${m.author?.kind === 'agent' ? ' agent' : ''}`; badge.textContent = actorDetail(m.author); meta.append(badge);
    const time = document.createElement('time'); time.className = 'message-time'; time.textContent = formatDate(m.created_at); time.dateTime = m.created_at || ''; meta.append(time); main.append(meta);
    const text = String(m.text ?? ''); const content = document.createElement('div'); content.className = 'message-text';
    const expanded = expandedMessages.has(m.id); content.textContent = text.length > 800 && !expanded ? `${text.slice(0, 800)}…` : text; main.append(content);
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
  async function loadMembers() { if (!state.project) return; const projectId = state.project.id; const generation = state.projectGeneration; const data = await api(`/projects/${encodeURIComponent(projectId)}/members`); if (generation === state.projectGeneration && state.project?.id === projectId) state.members = data.items || []; }
  async function loadActors() { const generation = state.authGeneration; try { const d = await api('/actors'); if (generation === state.authGeneration) state.actors = d.items || []; } catch { if (generation === state.authGeneration) state.actors = []; } }
  function isOwner() { return state.members.some(m => m.actor.id === state.me?.id && m.role === 'owner'); }
  function openModal(title, kicker, content) { ui.modalTitle.textContent = title; ui.modalKicker.textContent = kicker; ui.modalContent.replaceChildren(); ui.modalContent.append(content); if (!ui.modal.open) ui.modal.showModal(); }
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
    const wrap = document.createElement('div'); const p = document.createElement('p'); p.className = 'modal-copy'; p.textContent = isOwner() ? 'People with access to this project. Project owners can add participants and mute agents.' : 'People with access to this project.'; wrap.append(p);
    if (isOwner() && state.me.kind === 'human') {
      const available = state.actors.filter(a => !state.members.some(m => m.actor.id === a.id));
      if (available.length) {
        const form = document.createElement('form'); form.className = 'inline-form'; const select = document.createElement('select'); select.setAttribute('aria-label', 'Participant to add'); available.forEach(a => { const o = document.createElement('option'); o.value = a.id; o.textContent = `${actorLabel(a)} · ${actorDetail(a)}`; select.append(o); }); const button = document.createElement('button'); button.type = 'submit'; button.className = 'primary-button small'; button.textContent = 'Add person'; form.append(select, button); form.addEventListener('submit', async e => { e.preventDefault(); button.disabled = true; try { await api(`/projects/${encodeURIComponent(state.project.id)}/members`, { method: 'POST', body: { actor_id: select.value, role: 'member' } }); await showMembers(); } catch (err) { showError(err.message); button.disabled = false; } }); wrap.append(form);
      }
    }
    const list = document.createElement('div');
    if (!state.members.length) { const empty = document.createElement('div'); empty.className = 'empty-note'; empty.textContent = 'No project members found.'; list.append(empty); }
    for (const item of state.members) {
      const row = document.createElement('div'); row.className = 'member-row'; const av = document.createElement('div'); av.className = `message-avatar${item.actor.kind === 'agent' ? ' agent' : ''}`; av.textContent = initials(item.actor.name); const info = document.createElement('div'); info.className = 'member-info'; const name = document.createElement('div'); name.className = 'member-name'; name.textContent = actorLabel(item.actor); const sub = document.createElement('div'); sub.className = 'member-sub'; sub.textContent = `${actorDetail(item.actor)}${item.muted ? ' · muted' : ''}`; info.append(name, sub); const actions = document.createElement('div'); actions.className = 'member-actions'; const role = document.createElement('span'); role.className = 'role-tag'; role.textContent = item.role; actions.append(role);
      if (isOwner() && state.me.kind === 'human' && item.actor.kind === 'agent') { const toggle = document.createElement('button'); toggle.className = 'toggle'; toggle.textContent = item.muted ? 'Unmute' : 'Mute'; toggle.addEventListener('click', async () => { try { await api(`/projects/${encodeURIComponent(state.project.id)}/members/${encodeURIComponent(item.actor.id)}`, { method: 'PATCH', body: { muted: !item.muted } }); await showMembers(); } catch (e) { showError(e.message); } }); actions.append(toggle); }
      row.append(av, info, actions); list.append(row);
    }
    wrap.append(list); const footer = document.createElement('div'); footer.className = 'modal-actions';
    if (state.me.kind === 'human') { const make = document.createElement('button'); make.type = 'button'; make.className = 'secondary-button'; make.textContent = 'Create an agent'; make.addEventListener('click', createAgent); footer.append(make); }
    wrap.append(footer); openModal('People', state.project.name.toUpperCase(), wrap);
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
  $('#signin-form').addEventListener('submit', e => { e.preventDefault(); signIn($('#token-input').value); });
  $('#toggle-token').addEventListener('click', () => { const input = $('#token-input'); const shown = input.type === 'text'; input.type = shown ? 'password' : 'text'; $('#toggle-token').textContent = shown ? 'Show' : 'Hide'; $('#toggle-token').setAttribute('aria-label', shown ? 'Show token' : 'Hide token'); });
  $('#signout').addEventListener('click', () => signOut()); ui.composer.addEventListener('submit', sendMessage); ui.input.addEventListener('input', updateComposer); ui.input.addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ui.composer.requestSubmit(); } });
  $('#account-button').addEventListener('click', () => showMembers());
  const savedToken = state.token; if (savedToken) signIn(savedToken); else { ui.signin.hidden = false; ui.workspace.hidden = true; }
})();
