// Offline DOM unit checks for publicReplyDom (prepare + single-click submit); no browser connection.
import assert from 'node:assert/strict';
import vm from 'node:vm';
import {publicReplyDom} from './xhs_bridge_driver.mjs';

const OWN_UID = '1'.repeat(24);
const OTHER_UID = '2'.repeat(24);
const NOTE_ID = '3'.repeat(24);
const ROOT_CID = '4'.repeat(24);
const TARGET_CID = '5'.repeat(24);
const DIGEST = 'a'.repeat(64);
const URL_STR = `https://www.xiaohongshu.com/explore/${NOTE_ID}`;

function makeNode(tag, {cls = '', attrs = {}, text = '', children = [], visible = true, onClick = null, disabled = false} = {}) {
  const node = {
    tagName: tag.toUpperCase(),
    className: cls,
    _attrs: {...attrs},
    _text: text,
    value: text,
    disabled,
    children: [],
    parentElement: null,
    get textContent() { return this._text; },
    set textContent(v) { this._text = String(v); },
    get innerText() { return this._text; },
    set innerText(v) { this._text = String(v); },
    getAttribute(k) { return k in this._attrs ? this._attrs[k] : (k === 'class' ? this.className : null); },
    getClientRects() { return visible ? [{width: 100, height: 24}] : []; },
    contains(other) {
      if (other === this) return true;
      return this.children.some(c => c.contains(other));
    },
    closest(selector) {
      const parts = selector.split(',').map(s => s.trim());
      let cur = this;
      while (cur) {
        if (parts.some(p => matchSimple(cur, p))) return cur;
        cur = cur.parentElement;
      }
      return null;
    },
    querySelectorAll(selector) {
      const parts = selector.split(',').map(s => s.trim());
      const out = [];
      const walk = el => {
        for (const c of el.children) {
          if (parts.some(p => matchDescendant(c, p, this))) out.push(c);
          walk(c);
        }
      };
      walk(this);
      return out;
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
    focus() {},
    scrollIntoView() {},
    dispatchEvent() { return true; },
    click() { if (onClick) onClick(this); },
    append(...kids) {
      for (const k of kids) { k.parentElement = this; this.children.push(k); }
      return this;
    },
  };
  if (children.length) node.append(...children);
  return node;
}

function matchSimple(el, sel) {
  if (!sel) return false;
  if (sel.startsWith('#')) return el.getAttribute('id') === sel.slice(1);
  if (sel.includes('[href*="/user/profile/"]')) {
    return el.tagName === 'A' && String(el.getAttribute('href') || '').includes('/user/profile/');
  }
  if (sel.includes('[contenteditable="true"]')) {
    return el.getAttribute('contenteditable') === 'true';
  }
  if (sel.includes('[class*="reply"]')) return String(el.className || '').includes('reply');
  if (sel.includes('[class*="toast"]')) return String(el.className || '').includes('toast');
  if (sel.includes('[role="button"]')) return el.getAttribute('role') === 'button';
  if (sel.includes('[role="dialog"]')) return el.getAttribute('role') === 'dialog';
  const tokens = sel.split(/(?=[.#])/).filter(Boolean);
  for (const tok of tokens) {
    if (tok.startsWith('.')) {
      const classes = String(el.className || '').split(/\s+/);
      if (!classes.includes(tok.slice(1))) return false;
    } else if (tok.startsWith('#')) {
      if (el.getAttribute('id') !== tok.slice(1)) return false;
    } else if (el.tagName !== tok.toUpperCase()) {
      return false;
    }
  }
  return true;
}

function matchDescendant(el, sel, root) {
  const chain = sel.split(/\s+/).filter(Boolean);
  if (!matchSimple(el, chain.at(-1))) return false;
  if (chain.length === 1) return true;
  let cur = el.parentElement;
  for (let i = chain.length - 2; i >= 0; i--) {
    while (cur && cur !== root && !matchSimple(cur, chain[i])) cur = cur.parentElement;
    if (!cur || !matchSimple(cur, chain[i])) return false;
    cur = cur.parentElement;
  }
  return true;
}

function runDom(doc, opts, guardResult = {uid: OWN_UID, url: URL_STR, visible: true}) {
  const src = publicReplyDom.toString().replace('GUARD({...opts, guardOnly: true})', 'globalThis.__guard()');
  const ctx = {
    URL, Event: class { constructor(t) { this.type = t; } }, InputEvent: class { constructor(t) { this.type = t; } },
    setTimeout, document: doc, window: {getSelection: () => null}, location: {href: URL_STR},
    getComputedStyle: () => ({display: 'block', visibility: 'visible', opacity: '1'}),
    __guard: () => guardResult,
  };
  vm.createContext(ctx);
  return vm.runInContext(`(${src})(${JSON.stringify(opts)})`, ctx);
}

let checks = 0;

// 1. Reply-to-comment prepare + submit happy path (single click only)
{
  let replyClicks = 0, submitClicks = 0, composerVisible = false, composerHint = '';
  const composer = makeNode('div', {attrs: {id: 'content-textarea', contenteditable: 'true'}, visible: false});
  Object.defineProperty(composer, 'getClientRects', {value: () => composerVisible ? [{width: 200, height: 30}] : []});
  const hint = makeNode('div', {cls: 'reply-info', text: '', visible: false});
  Object.defineProperty(hint, 'getClientRects', {value: () => composerHint ? [{width: 100, height: 18}] : []});
  const submitBtn = makeNode('button', {
    cls: 'submit', text: '发送', visible: false,
    onClick: () => { submitClicks++; composer.textContent = ''; },
  });
  Object.defineProperty(submitBtn, 'getClientRects', {value: () => composerVisible ? [{width: 60, height: 28}] : []});
  const replyBtn = makeNode('span', {
    cls: 'reply', text: '回复',
    onClick: () => { replyClicks++; composerVisible = true; composerHint = '回复 Synthetic Author'; hint.textContent = composerHint; },
  });
  const rootAuthorLink = makeNode('a', {cls: 'name', attrs: {href: `/user/profile/${'9'.repeat(24)}`}, text: 'Root Author'});
  const rootItem = makeNode('div', {
    cls: 'comment-item', attrs: {'data-comment-id': ROOT_CID},
    children: [
      makeNode('div', {cls: 'author-wrapper', children: [rootAuthorLink]}),
      makeNode('div', {cls: 'content', text: 'Synthetic root comment'}),
    ],
  });
  const authorLink = makeNode('a', {cls: 'name', attrs: {href: `/user/profile/${OTHER_UID}`}, text: 'Synthetic Author'});
  const targetItem = makeNode('div', {
    cls: 'comment-item-sub', attrs: {'data-comment-id': TARGET_CID},
    children: [
      makeNode('div', {cls: 'author-wrapper', children: [authorLink]}),
      makeNode('div', {cls: 'content', text: 'Synthetic invitation'}),
      makeNode('div', {cls: 'interactions', children: [replyBtn]}),
    ],
  });
  const replyContainer = makeNode('div', {cls: 'reply-container', children: [targetItem]});
  const parent = makeNode('div', {cls: 'parent-comment', attrs: {'data-comment-id': ROOT_CID}, children: [rootItem, replyContainer]});
  const comments = makeNode('div', {cls: 'comments-container', children: [parent]});
  const bar = makeNode('div', {cls: 'engage-bar-container', children: [hint, composer, submitBtn]});
  const root = makeNode('div', {cls: 'note-container', children: [comments, bar]});
  const doc = makeNode('body', {children: [root]});

  const baseOpts = {
    expectedPath: `/explore/${NOTE_ID}`, uid: OWN_UID, destination: 'reply_to_comment',
    recipientUid: OTHER_UID, recipientName: 'Synthetic Author',
    recipientCommentId: TARGET_CID, rootCommentId: ROOT_CID,
    message: 'Synthetic inquiry?', postClickPollRounds: 1, postClickPollMs: 0,
  };
  const prep = await runDom(doc, {...baseOpts, action: 'public_reply_prepare'});
  assert.equal(prep.readyForSubmit, true);
  assert.equal(prep.sendClicked, false);
  assert.equal(replyClicks, 0);
  checks++;

  const sub = await runDom(doc, {...baseOpts, action: 'public_reply_submit', execute: true, intentDigest: DIGEST});
  assert.equal(sub.sendClicked, true);
  assert.equal(sub.clicksSubmitted, 1);
  assert.equal(replyClicks, 1);
  assert.equal(submitClicks, 1);
  assert.equal(sub.composerCleared, true);
  assert.equal(sub.captchaObserved, false);
  checks++;
}

// 2. Top-level comment refuses stale reply target banner (zero send clicks)
{
  let submitClicks = 0;
  const noteAuthor = makeNode('a', {attrs: {href: `/user/profile/${OTHER_UID}`}, text: 'Author'});
  const composer = makeNode('div', {attrs: {id: 'content-textarea', contenteditable: 'true'}, visible: true});
  const staleHint = makeNode('div', {cls: 'reply-info', text: '回复 WrongPerson', visible: true});
  const submitBtn = makeNode('button', {cls: 'submit', text: '发送', visible: true, onClick: () => { submitClicks++; }});
  const bar = makeNode('div', {cls: 'engage-bar-container', children: [staleHint, composer, submitBtn]});
  const root = makeNode('div', {cls: 'note-container', children: [makeNode('div', {cls: 'author', children: [noteAuthor]}), bar]});
  const doc = makeNode('body', {children: [root]});

  const res = await runDom(doc, {
    action: 'public_reply_submit', expectedPath: `/explore/${NOTE_ID}`, uid: OWN_UID,
    destination: 'top_level_comment', recipientUid: OTHER_UID,
    message: 'Top-level question?', execute: true, intentDigest: DIGEST,
    postClickPollRounds: 1, postClickPollMs: 0,
  });
  assert.equal(res.sendClicked, false);
  assert.equal(res.gap, 'STALE_REPLY_TARGET_ACTIVE');
  assert.equal(submitClicks, 0);
  checks++;
}

// 3. Captcha modal observed after click reports captchaObserved=true
{
  let showCaptcha = false;
  const noteAuthor = makeNode('a', {attrs: {href: `/user/profile/${OTHER_UID}`}, text: 'Author'});
  const composer = makeNode('div', {attrs: {id: 'content-textarea', contenteditable: 'true'}, visible: true});
  const submitBtn = makeNode('button', {cls: 'submit', text: '发送', visible: true, onClick: () => { showCaptcha = true; }});
  const modal = makeNode('div', {cls: 'captcha-container', text: '请使用App扫码验证', visible: false});
  Object.defineProperty(modal, 'getClientRects', {value: () => showCaptcha ? [{width: 300, height: 300}] : []});
  const bar = makeNode('div', {cls: 'engage-bar-container', children: [composer, submitBtn]});
  const root = makeNode('div', {cls: 'note-container', children: [makeNode('div', {cls: 'author', children: [noteAuthor]}), bar]});
  const doc = makeNode('body', {children: [root, modal]});

  const res = await runDom(doc, {
    action: 'public_reply_submit', expectedPath: `/explore/${NOTE_ID}`, uid: OWN_UID,
    destination: 'top_level_comment', recipientUid: OTHER_UID,
    message: 'Top-level question?', execute: true, intentDigest: DIGEST,
    postClickPollRounds: 1, postClickPollMs: 0,
  });
  assert.equal(res.sendClicked, true);
  assert.equal(res.captchaObserved, true);
  assert.match(res.accessWarningText, /扫码验证/);
  checks++;
}

// 4. Rejects missing execute flag or recipient UID mismatch
{
  const doc = makeNode('body', {children: [makeNode('div', {cls: 'note-container'})]});
  const res = await runDom(doc, {
    action: 'public_reply_submit', expectedPath: `/explore/${NOTE_ID}`, uid: OWN_UID,
    destination: 'top_level_comment', recipientUid: OTHER_UID,
    message: 'Question?', execute: false, intentDigest: DIGEST,
  });
  assert.equal(res.sendClicked, false);
  assert.equal(res.gap, 'NOTE_AUTHOR_UID_MISMATCH');
  checks++;
}

// 5. Inactive engage-bar with mounted #content-textarea must require active banner before sending
{
  let submitClicks = 0;
  const composer = makeNode('p', {attrs: {id: 'content-textarea', contenteditable: 'true'}, visible: true});
  const inactiveCover = makeNode('div', {cls: 'not-active inner-when-not-active', text: '说点什么...', visible: true});
  const submitBtn = makeNode('button', {cls: 'submit', text: '发送', visible: true, onClick: () => { submitClicks++; }});
  const engageBar = makeNode('div', {cls: 'engage-bar', children: [composer, inactiveCover, submitBtn]});
  const replyBtn = makeNode('div', {cls: 'reply icon-container', text: '回复', onClick: () => {
    // Simulates reply click activating engage-bar WITHOUT setting reply-content banner
    engageBar.className = 'engage-bar active';
    inactiveCover.getClientRects = () => [];
  }});
  const authorLink = makeNode('a', {cls: 'name', attrs: {href: `/user/profile/${OTHER_UID}`}, text: 'Target Person'});
  const rootItem = makeNode('div', {
    cls: 'comment-item', attrs: {'data-comment-id': ROOT_CID},
    children: [
      makeNode('div', {cls: 'author-wrapper', children: [authorLink]}),
      makeNode('div', {cls: 'content', text: 'Original'}),
      makeNode('div', {cls: 'interactions', children: [replyBtn]}),
    ],
  });
  const parent = makeNode('div', {cls: 'parent-comment', attrs: {'data-comment-id': ROOT_CID}, children: [rootItem]});
  const doc = makeNode('body', {children: [makeNode('div', {cls: 'note-container', children: [
    makeNode('div', {cls: 'comments-container', children: [parent]}),
    makeNode('div', {cls: 'engage-bar-container', children: [engageBar]}),
  ]})]});

  const res = await runDom(doc, {
    action: 'public_reply_submit', expectedPath: `/explore/${NOTE_ID}`, uid: OWN_UID,
    destination: 'reply_to_comment', recipientUid: OTHER_UID, recipientName: 'Target Person',
    recipientCommentId: ROOT_CID, rootCommentId: ROOT_CID,
    message: 'Synthetic question?', execute: true, intentDigest: DIGEST,
    postClickPollRounds: 1, postClickPollMs: 0,
  });
  assert.equal(res.sendClicked, false);
  assert.equal(res.gap, 'REPLY_TARGET_HINT_MISMATCH');
  assert.equal(submitClicks, 0);
  checks++;
}

console.log(`PASS: guarded public reply DOM checks: ${checks}`);
