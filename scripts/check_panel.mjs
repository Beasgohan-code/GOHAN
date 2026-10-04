#!/usr/bin/env node
/**
 * Headless smoke test for the control panel.
 *
 * There is no browser in CI, and `node --check` only proves the file parses -
 * which is exactly how a `PAGES = { render: pageSomethingMissing }` typo shipped
 * a panel that never booted. This harness evaluates `app.js` in a sandbox with a
 * minimal DOM, lets it talk to a running panel over HTTP, then renders **every**
 * page and reports what blew up.
 *
 *   node scripts/check_panel.mjs                 # against http://127.0.0.1:8080
 *   node scripts/check_panel.mjs http://host:80  # against another panel
 */

import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = dirname(fileURLToPath(import.meta.url));
const appJsPath = resolve(here, '../gohan/web/static/app.js');
const base = (process.argv[2] || process.env.PANEL_URL || 'http://127.0.0.1:8080').replace(/\/$/, '');

/* ------------------------------------------------------------------ DOM --- */

const gradient = { addColorStop() {} };

function canvasContext() {
  const target = {};
  return new Proxy(target, {
    get: (store, key) => {
      if (key in store) return store[key];
      if (key === 'createLinearGradient' || key === 'createRadialGradient') return () => gradient;
      if (key === 'measureText') return () => ({ width: 24 });
      return () => undefined;
    },
    set: (store, key, value) => {
      store[key] = value;
      return true;
    },
  });
}

function makeNode(tag = 'div') {
  const node = {
    tagName: String(tag).toUpperCase(),
    style: {},
    dataset: {},
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    children: [],
    value: '',
    checked: false,
    hidden: false,
    disabled: false,
    textContent: '',
    innerHTML: '',
    dataset_: {},
    width: 640,
    height: 220,
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent() {},
    appendChild(child) {
      node.children.push(child);
      return child;
    },
    removeChild(child) {
      node.children = node.children.filter((entry) => entry !== child);
      return child;
    },
    replaceChildren() {},
    insertAdjacentHTML() {},
    setAttribute() {},
    removeAttribute() {},
    getAttribute: () => null,
    focus() {},
    blur() {},
    click() {},
    matches: () => false,
    closest: () => null,
    scrollIntoView() {},
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 640, height: 220 }),
    getContext: () => canvasContext(),
    querySelector: () => makeNode('span'),
    querySelectorAll: () => [],
  };
  return node;
}

function makeDocument() {
  const nodes = new Map();
  return {
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, makeNode());
      return nodes.get(id);
    },
    querySelector(selector) {
      return makeDocument_nodes(selector);
    },
    querySelectorAll: () => [],
    createElement: (tag) => makeNode(tag),
    createElementNS: (_ns, tag) => makeNode(tag),
    addEventListener() {},
    removeEventListener() {},
    documentElement: makeNode('html'),
    body: makeNode('body'),
    activeElement: makeNode(),
    hidden: false,
    title: '',
  };
}

function makeDocument_nodes(selector) {
  return makeDocument_nodes.cache.get(selector) || (makeDocument_nodes.cache.set(selector, makeNode()), makeDocument_nodes.cache.get(selector));
}
makeDocument_nodes.cache = new Map();

/* --------------------------------------------------------------- sandbox --- */

const listeners = new Set();

const sandbox = {
  console,
  document: { ...makeDocument() },
  window: {
    devicePixelRatio: 1,
    addEventListener() {},
    removeEventListener() {},
    matchMedia: () => ({ matches: false, addEventListener() {}, addListener() {} }),
    location: { hash: '', reload() {}, href: base },
    history: { replaceState() {}, pushState() {} },
    localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
    open() {},
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
  },
  navigator: { clipboard: { writeText: async () => {} }, userAgent: 'node', language: 'en' },
  location: { hash: '', reload() {}, href: `${base}/`, origin: base, pathname: '/' },
  history: { replaceState() {}, pushState() {} },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  matchMedia: () => ({ matches: false, addEventListener() {} }),
  requestAnimationFrame: (fn) => setTimeout(() => fn(Date.now()), 0),
  getComputedStyle: () => ({
    // drawChart reads the theme's CSS variables
    getPropertyValue: (name) =>
      ({ '--line': '#2a2f3a', '--accent': '#7c9cff', '--mut': '#8b93a7', '--ok': '#4ade80', '--danger': '#f87171' })[name] || '#888888',
  }),
  cancelAnimationFrame: clearTimeout,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  queueMicrotask,
  AbortController,
  EventSource: class {
    constructor(url) {
      this.url = url;
      listeners.add(this);
    }
    addEventListener() {}
    removeEventListener() {}
    close() {}
  },
  fetch: async (url, options) => {
    const target = String(url).startsWith('http') ? String(url) : `${base}${String(url).startsWith('/') ? '' : '/'}${url}`;
    return fetch(target, options);
  },
  alert: () => {},
  confirm: () => true,
  prompt: () => null,
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
sandbox.window.document = sandbox.document;
sandbox.window.fetch = sandbox.fetch;

const context = vm.createContext(sandbox);
const source = readFileSync(appJsPath, 'utf8');

let failures = 0;
const line = (ok, what, detail = '') =>
  console.log(`${ok ? '  \u2713' : '  \u2717'} ${what}${detail ? ` ${ok ? '' : `- ${detail}`}` : ''}`);

try {
  vm.runInContext(source, context, { filename: appJsPath });
  line(true, 'app.js evaluated (no ReferenceError at load)');
} catch (error) {
  line(false, 'app.js evaluated', error.message);
  process.exit(1);
}

const report = await vm.runInContext(
  `(async () => {
     const out = { pages: [], booted: false, badge: null };
     const loaders = {
       overview: loadOverview,
       modules: loadOverview,
       music: loadMusic,
       moderation: loadModeration,
       users: () => loadUsers('', 'all'),
       groups: () => loadGroups(''),
       events: loadEvents,
       settings: loadSettings,
     };
     try { await boot(); out.booted = true; } catch (error) { out.bootError = String(error && error.stack || error); }
     for (const name of Object.keys(PAGES)) {
       const entry = { name };
       try {
         const loader = loaders[name];
         if (loader) await loader();
       } catch (error) { entry.load = String(error && error.stack || error).split('\\n').slice(0, 2).join(' '); }
       try {
         App.page = name;
         render();
         if (!$('#view').innerHTML || !String($('#view').innerHTML).trim()) entry.render = 'rendered an empty page';
       } catch (error) { entry.render = String(error && error.stack || error).split('\\n').slice(0, 2).join(' '); }
       out.pages.push(entry);
     }
     try { updateBadges(); out.badge = 'ok'; } catch (error) { out.badge = String(error.message); }
     return JSON.stringify(out);
   })()`,
  context,
);

const result = JSON.parse(report);
line(result.booted, `boot() completed${result.bootError ? `- ${result.bootError}` : ''}`);
if (result.bootError) failures += 1;
line(result.badge === 'ok', `updateBadges() ${result.badge === 'ok' ? 'completed' : `- ${result.badge}`}`);
if (result.badge !== 'ok') failures += 1;

for (const page of result.pages) {
  const problem = page.load || page.render;
  line(!problem, `page ${page.name}`, problem || '');
  if (problem) failures += 1;
}

console.log(failures ? `\n  ${failures} panel problem(s) - the browser would break here.\n` : '\n  panel checked: every page renders.\n');
process.exit(failures ? 1 : 0);
