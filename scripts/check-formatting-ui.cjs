#!/usr/bin/env node
'use strict';
// DOM-only checks for message formatting (code, math) and project search; use an isolated jsdom install through JSDOM_PATH.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { createRequire } = require('node:module');
const { JSDOM } = process.env.JSDOM_PATH
  ? createRequire(path.resolve(process.env.JSDOM_PATH, 'package.json'))('jsdom') : require('jsdom');
const directory = path.resolve(__dirname, '../src/agent_commons/static');
const html = fs.readFileSync(path.join(directory, 'index.html'), 'utf8');
const source = fs.readFileSync(path.join(directory, 'app.js'), 'utf8');
assert.match(html, /vendor\/katex\/katex\.min\.js" defer/); assert.match(html, /vendor\/katex\/katex\.min\.css/); assert.doesNotMatch(html, /https?:\/\/(?!workspace\.test)/, 'no external resources');
const me = { id: 'human', name: 'Researcher', handle: 'researcher', kind: 'human', owner: null, has_password: true };
const project = { id: 'project', name: 'Study', description: '' };
const channels = [{ id: 'c1', project_id: 'project', name: 'general' }, { id: 'c2', project_id: 'project', name: 'results' }];
const threads = { c1: [{ id: 't1', channel_id: 'c1', title: 'Intro' }], c2: [{ id: 't2', channel_id: 'c2', title: 'Findings' }] };
const tick = () => new Promise(resolve => setTimeout(resolve, 0));
const flush = async () => { for (let i = 0; i < 8; i++) await tick(); };
const msg = (id, text, extra = {}) => ({ id, thread_id: 't2', project_id: 'project', text, sequence: Number(id.slice(1)), author: me, reactions: [], reply_to: null, reply_count: 0, created_at: '2026-01-02T03:04:05+00:00', mentions: [], ...extra });
const hits = n => Array.from({ length: n }, (_, i) => ({ message: msg(`m${100 - i}`, `finding ${i}`), thread: { id: 't2', title: 'Findings', channel_id: 'c2' }, snippet: `…finding ${i}…` }));

async function create({ katex = true, messages = [] } = {}) {
  const dom = new JSDOM(html, { url: 'https://workspace.test', runScripts: 'outside-only', pretendToBeVisual: true }); const w = dom.window; const requests = [];
  w.Headers = Headers; w.setInterval = () => 1; w.clearInterval = () => {};
  w.HTMLDialogElement.prototype.showModal = function () { this.open = true; }; w.HTMLDialogElement.prototype.close = function () { this.open = false; };
  w.Element.prototype.scrollIntoView = function () { w.__scrolled = this; };
  const rendered = []; if (katex) w.katex = { render(tex, el, options) { rendered.push({ tex, options }); const span = w.document.createElement('span'); span.className = 'katex'; span.textContent = `[${tex}]`; el.append(span); } };
  const searchPages = [{ items: hits(2), next_before: 99, cursor: 200 }, { items: hits(1), next_before: null, cursor: 200 }];
  w.fetch = async (url, options = {}) => {
    requests.push({ url, ...options }); let data = null;
    if (url === '/v1/me') data = me; else if (url === '/v1/projects') data = { items: [project] };
    else if (url.includes('/search')) data = searchPages.shift();
    else if (/\/projects\/project\/channels$/.test(url)) data = { items: channels };
    else if (url.includes('/members') || url === '/v1/actors') data = { items: [] };
    else if (url.includes('/events')) data = { items: [], cursor: 5, next_cursor: null };
    else if (/\/channels\/(c\d)\/threads/.test(url)) data = { items: threads[/channels\/(c\d)/.exec(url)[1]] };
    else if (/\/threads\/t\d\/messages/.test(url)) data = { items: messages, cursor: 5, next_cursor: null };
    else data = { items: [] };
    return { status: 200, ok: true, json: async () => structuredClone(data) };
  };
  w.eval(source.replace('  const savedToken = state.token;', '  window.__t = { state, ui, renderMessages, selectProject };\n  const savedToken = state.token;'));
  await flush(); return { w, d: w.document, requests, rendered, ...w.__t };
}
const render = (t, texts) => { t.state.messages = texts.map((text, i) => msg(`m${i + 1}`, text)); t.renderMessages(true); return [...t.ui.messages.querySelectorAll('.message-text')]; };

(async () => {
  const t = await create();
  // Fenced block: pre/code, language label, copy button, no math or inline-code parsing inside.
  let [el] = render(t, ['before\n```python\nx = "$a$ and `b`"\n$$y$$\n```\nafter $x^2$']);
  const pre = el.querySelector('pre > code'); assert.ok(pre, 'fenced block becomes pre>code'); assert.equal(pre.textContent, 'x = "$a$ and `b`"\n$$y$$');
  assert.equal(el.querySelector('.code-lang').textContent, 'python'); assert.equal(el.querySelector('.code-copy').textContent, 'Copy');
  assert.deepEqual(t.rendered.map(r => r.tex), ['x^2'], 'math only outside of code'); assert.equal(pre.querySelector('*'), null);
  assert.equal(el.querySelectorAll('.inline-code').length, 0); assert.match(el.textContent, /before/); assert.match(el.textContent, /after/);
  let copied = null; t.w.navigator.clipboard = undefined; Object.defineProperty(t.w.navigator, 'clipboard', { value: { writeText: async value => { copied = value; } }, configurable: true });
  el.querySelector('.code-copy').click(); await flush(); assert.equal(copied, 'x = "$a$ and `b`"\n$$y$$'); assert.equal(el.querySelector('.code-copy').textContent, 'Copied');
  // No language, unclosed fence, tilde-free text, and a fence-like line that is not a fence.
  [el] = render(t, ['```\nplain\n```']); assert.equal(el.querySelector('.code-lang').textContent, ''); assert.equal(el.querySelector('code').textContent, 'plain');
  [el] = render(t, ['```js\nunclosed $x$']); assert.equal(el.querySelector('code').textContent, 'unclosed $x$');
  // Inline code protects math.
  t.rendered.length = 0; [el] = render(t, ['use `$x^2$` here and `a < b`']);
  assert.deepEqual([...el.querySelectorAll('code.inline-code')].map(c => c.textContent), ['$x^2$', 'a < b']); assert.equal(t.rendered.length, 0); assert.equal(el.textContent, 'use $x^2$ here and a < b');
  // Inline math goes through katex.render with the safe options.
  [el] = render(t, ['Energy $x^2$ and \\(y_1\\) done']);
  assert.deepEqual(t.rendered.map(r => r.tex), ['x^2', 'y_1']); assert.equal(el.querySelectorAll('.math-inline .katex').length, 2);
  assert.deepEqual({ ...t.rendered[0].options }, { throwOnError: false, trust: false, strict: 'ignore', maxExpand: 1000, maxSize: 20, displayMode: false });
  // Display math, both delimiters, multi-line.
  t.rendered.length = 0; [el] = render(t, ['a\n$$\\int_0^1 f\\,dx$$\nb \\[ z \\] c']);
  assert.deepEqual(t.rendered.map(r => [r.tex, r.options.displayMode]), [['\\int_0^1 f\\,dx', true], ['z', true]]); assert.equal(el.querySelectorAll('.math-display').length, 2);
  assert.ok(!el.textContent.includes('$$') && !el.textContent.includes('\\['));
  // Currency stays text.
  t.rendered.length = 0; [el] = render(t, ['It costs $5 and $10 total', 'also $ x $ and 3$4']);
  assert.equal(t.rendered.length, 0, 'prices and spaced dollars are not math'); assert.equal(el.textContent, 'It costs $5 and $10 total');
  assert.equal(t.ui.messages.querySelectorAll('.message-text')[1].textContent, 'also $ x $ and 3$4');
  [el] = render(t, ['pay \\$5 and $y$.']); assert.deepEqual(t.rendered.map(r => r.tex), ['y']); assert.equal(el.textContent.startsWith('pay $5 and '), true);
  // Script text and HTML stay inert; newlines are preserved.
  t.rendered.length = 0; [el] = render(t, ['<script>window.pwned=1</script><img src=x onerror="window.pwned=1">\nline two']);
  assert.equal(el.querySelector('script, img'), null); assert.equal(t.w.pwned, undefined); assert.equal(el.textContent, '<script>window.pwned=1</script><img src=x onerror="window.pwned=1">\nline two');
  [el] = render(t, ['```\n<script>1</script>\n```\n$<img src=x onerror=1>$']); assert.equal(el.querySelector('script, img'), null); assert.equal(el.querySelector('pre code').textContent, '<script>1</script>');
  // Long messages: the preview is shortened, expanding restores everything.
  const long = `${'word '.repeat(170)}\n\`\`\`\ncode block\n\`\`\``; [el] = render(t, [long]);
  const toggle = t.ui.messages.querySelector('.detail-toggle'); assert.ok(toggle); assert.equal(el.querySelector('pre'), null); assert.ok(el.textContent.endsWith('…'));
  toggle.click(); el = t.ui.messages.querySelector('.message-text'); assert.equal(el.querySelector('pre code').textContent, 'code block'); assert.equal(t.ui.messages.querySelector('.detail-toggle').textContent, 'Show less');
  // Composer hint.
  assert.match(t.d.querySelector('.format-hint').textContent, /``` for code · \$…\$ for math/);
  t.w.close();

  // Without KaTeX the raw TeX is shown in a code element.
  const bare = await create({ katex: false }); [el] = render(bare, ['say $a^2$ and $$b_1$$']);
  assert.deepEqual([...el.querySelectorAll('code.math-raw')].map(c => c.textContent), ['a^2', 'b_1']); bare.w.close();
  // A throwing renderer falls back to raw TeX too.
  const broken = await create(); broken.w.katex.render = () => { throw new Error('boom'); }; [el] = render(broken, ['$q$']); assert.equal(el.querySelector('code.math-raw').textContent, 'q'); broken.w.close();

  // Search: hidden until a project is open, results panel, load more, jump to message.
  const s = await create({ messages: [msg('m100', 'finding 0'), msg('m99', 'other')] }); const d = s.d;
  assert.match(html, /id="search-form"[^>]*\shidden/, 'search starts hidden until a project opens'); await s.selectProject(project); await flush(); assert.equal(d.querySelector('#search-form').hidden, false);
  d.querySelector('#search-input').value = '  finding  '; d.querySelector('#search-form').dispatchEvent(new s.w.Event('submit', { bubbles: true, cancelable: true })); await flush();
  assert.equal(d.querySelector('#modal').open, true); assert.equal(d.querySelector('#modal-title').textContent, 'Search results');
  const call = s.requests.find(r => r.url.includes('/search')); assert.equal(call.url, '/v1/projects/project/search?q=finding&limit=20');
  assert.equal(d.querySelectorAll('.search-result').length, 2); assert.match(d.querySelector('.search-result').textContent, /finding 0.*Findings.*Researcher/);
  const more = d.querySelector('.search-more'); assert.equal(more.hidden, false); more.click(); await flush();
  assert.match(s.requests.filter(r => r.url.includes('/search')).at(-1).url, /before=99/); assert.equal(d.querySelectorAll('.search-result').length, 3); assert.equal(more.hidden, true);
  d.querySelector('.search-result').click(); await flush();
  assert.equal(d.querySelector('#modal').open, false); assert.equal(s.state.channel.id, 'c2'); assert.equal(s.state.thread.id, 't2'); assert.equal(d.querySelector('#channel-title').textContent, 'results');
  const target = d.querySelector('[data-message-id="m100"]'); assert.ok(target.classList.contains('search-hit')); assert.equal(s.w.__scrolled, target);
  s.w.close();
  console.log('Formatting DOM checks passed: fenced code, inline code, KaTeX inline/display, currency, inert HTML, long messages, fallback, search results and jump.');
})().catch(error => { console.error(error); process.exitCode = 1; });
