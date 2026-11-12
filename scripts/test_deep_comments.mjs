// Offline behavioral checks against a tiny synthetic rendered comment tree.
import assert from 'node:assert/strict';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import vm from 'node:vm';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import { buildDeepExtractor, classifyReadError } from './deep_comments.mjs';

const pkg = process.argv[2];
if (!pkg) throw new Error('Pass the installed OpenCLI package directory');
const { buildCommentsExtractJs } = await import(pathToFileURL(path.join(path.resolve(pkg), 'clis/xiaohongshu/comments.js')).href);

function fixture(blocked = false, options = {}) {
  const location = {href: 'https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa'};
  const state = { replies: 0, clicks: 0, roots: 1, scrolls: 0, pending: false, ticks: 0, now: 10000, clickTimes: [], timerCalls: 0 };
  class Element {
    constructor(text = '') { this.textContent = text; }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    getClientRects() { return [{}]; }
    contains() { return false; }
    scrollIntoView() {}
  }
  const authorLink = new Element();
  authorLink.getAttribute = () => '/user/profile/aaaaaaaaaaaaaaaaaaaaaaaa';
  const item = (name, content) => {
    const element = new Element();
    if (options.realIds) element.getAttribute = attribute => attribute === 'data-comment-id'
      ? (name === 'Root author' ? 'a'.repeat(24) : (Number(name.split(' ').at(-1)) + 1).toString(16).padStart(24, '0')) : '';
    element.querySelector = selector => {
      if (selector.includes('href*=')) return authorLink;
      if (selector.includes('.nickname')) return null;
      if (selector.includes('.name') || selector.includes('.user-name')) return new Element(name);
      if (selector.includes('.content') || selector.includes('.note-text')) return new Element(content);
      if (selector.includes('.count')) return new Element('0');
      if (selector.includes('.date') || selector.includes('.time')) return new Element('2026-10-30');
      return null;
    };
    return element;
  };
  const rootItem = item('Root author', 'Which doctor?');
  const button = new Element('展开更多回复');
  button.getAttribute = name => options.linkExpander && name === 'href' ? '/chat/aaaaaaaaaaaaaaaaaaaaaaaa' : '';
  button.click = () => {
    if (options.navigateOnExpand) location.href = 'https://www.xiaohongshu.com/chat/aaaaaaaaaaaaaaaaaaaaaaaa';
    state.clicks++; if (!options.deferReplies) state.replies++;
    state.clickTimes.push(state.now);
    if (options.lateRoot) state.roots = 2;
  };
  const root = new Element();
  root.querySelector = selector => {
    if (selector === '.comment-item') return rootItem;
    if (selector.includes('.name')) return new Element('Root author');
    if (selector.includes('.content')) return new Element('Which doctor?');
    return null;
  };
  root.querySelectorAll = selector => {
    if (selector.startsWith('button')) return state.replies < 3 ? [button] : [];
    if (selector.includes('.comment-item-sub')) return Array.from({length: state.replies}, (_, i) =>
      item('Reply ' + i, options.replyText ? options.replyText(i, state) : 'Reply page ' + i));
    return [];
  };
  const roots = [root, Object.assign(new Element(), root), Object.assign(new Element(), root)];
  const scroller = {tagName: 'DIV', className: 'note-scroller', scrollHeight: 1000, clientHeight: 400, clientWidth: 300,
    getBoundingClientRect: () => ({top: 20, left: 10, width: 300, height: 400, bottom: 420, right: 310}), scrollTo() {
    if (options.postExpansionRoots && state.replies < 3) return;
    state.scrolls++; state.pending = true; state.ticks = 0;
  }};
  const noteScope = new Element();noteScope.contains = element => element === scroller;
  const document = {
    body: { innerText: blocked ? '请求太频繁' : '', scrollHeight: 0 },
    querySelector: selector => selector.includes('.total') ? new Element(options.lateRoot ? '共1条评论' : '共4条评论')
      : selector.includes('#noteContainer') ? noteScope
      : selector === '.note-scroller' && (options.delayedRoots || options.postExpansionRoots) ? scroller : null,
    querySelectorAll: selector => selector === '.parent-comment' ? roots.slice(0, state.roots) : [],
  };
  Object.defineProperty(document.body, 'innerText', {get() {
    if (options.rejectPrivateRead && location.href.includes('/chat/')) throw new Error('PRIVATE_BODY_WAS_READ');
    return options.wallText || (blocked ? '请求太频繁' : '');
  }});
  return { state, context: { document, HTMLElement: Element, location,
    window: {innerWidth: 800, innerHeight: 600, getComputedStyle: element => ({overflowY: element === scroller ? 'scroll' : 'visible'}), scrollTo() {}}, setTimeout(callback, milliseconds) {
      state.timerCalls++;
      if (options.neverCallback) return;
      if (options.advancingClock) state.now += milliseconds;
      if (state.pending && ++state.ticks >= 7) {
        state.roots = Math.min(3, state.roots + 1); state.pending = false;
      }
      callback();
    }, Date: options.advancingClock ? class extends Date {static now() {return state.now;}} : Date, Math } };
}

const full = fixture();
const result = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {roots: 1, maxReplyClicks: 10, budgetMs: 10000}), full.context);
assert.equal(full.state.clicks, 3, 'A repeated button label must not suppress later reply pages');
assert.equal(result.results.filter(x => x.is_reply).length, 3);
assert.equal(result.deepCoverage.remainingThreads.length, 1, 'Captured text without real root/reply IDs must retain an identity gap');
assert.equal(result.deepCoverage.threads[0].reason, 'root_identity_unconfirmed');

const partial = fixture();
const limited = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {roots: 1, maxReplyClicks: 1, budgetMs: 10000}), partial.context);
assert.equal(partial.state.clicks, 1);
assert.equal(limited.deepCoverage.remainingThreads.length, 1);
assert.equal(limited.deepCoverage.clickBudgetReached, true);

const blocked = fixture(true);
await assert.rejects(vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {roots: 1, maxReplyClicks: 10, budgetMs: 10000}), blocked.context), /XHS_ACCESS_RESTRICTED/);
assert.equal(blocked.state.clicks, 0);
for (const wallText of ['登录查看全部评论内容', '登录查看全部评论', '登录后查看全部评论内容', '登录查看更多评论', '登录查看更多', '登录即可查看', 'Log in to view comments']) {
  const wall = fixture(false, {wallText});
  await assert.rejects(vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
    roots: 1, maxReplyClicks: 10, budgetMs: 10000,
  }), wall.context), /XHS_ACCESS_RESTRICTED/, wallText);
  assert.equal(wall.state.clicks, 0, 'Login walls must stop before any reply click: ' + wallText);
}

assert.throws(() => buildDeepExtractor(() => 'changed upstream adapter'), /Adapter source changed/);

const delayed = fixture(false, {delayedRoots: true});
const delayedResult = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {roots: 3, maxReplyClicks: 0, budgetMs: 10000}), delayed.context);
assert.equal(delayed.state.scrolls, 2);
assert.equal(delayedResult.results.filter(x => !x.is_reply).length, 3, 'Wait for slow asynchronous root batches');

const late = fixture(false, {lateRoot: true});
const lateResult = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {roots: 3, maxReplyClicks: 10, budgetMs: 10000}), late.context);
assert.equal(lateResult.results.filter(x => !x.is_reply).length, 2, 'Include roots arriving during the last reply expansion');
const postExpansion = fixture(false, {postExpansionRoots: true});
const postResult = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {roots: 3, maxReplyClicks: 10, budgetMs: 10000}), postExpansion.context);
assert.equal(postResult.results.filter(x => !x.is_reply).length, 3, 'Revisit the bottom after reply expansion changes scroll geometry');
assert.equal(classifyReadError('Inspected target navigated or closed'), 'ACCESS_STATE_UNCONFIRMED');
assert.equal(classifyReadError('XHS_ACCESS_RESTRICTED'), 'SECURITY_BLOCK');
assert.equal(classifyReadError('Unexpected adapter schema'), 'DEEP_READ_ERROR');

const paced = fixture(false, {advancingClock: true});
await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {roots: 1, maxReplyClicks: 10, budgetMs: 10000}), paced.context);
assert.equal(paced.state.clickTimes.length, 3);
assert.ok(paced.state.clickTimes.slice(1).every((time, i) => time - paced.state.clickTimes[i] >= 3000));

function manyRootsFixture(count = 245, reverse = false) {
  let clock = 10000;
  const clickedIds = [];
  class Element {
    constructor(text = '', attributes = {}) { this.textContent = text; this.attributes = attributes; }
    getAttribute(name) { return this.attributes[name] || ''; }
    querySelector() { return null; }
    querySelectorAll() { return []; }
    getClientRects() { return [{}]; }
    contains() { return false; }
    scrollIntoView() {}
  }
  const ids = Array.from({length: count}, (_, i) => (i + 1).toString(16).padStart(24, '0'));
  const parents = ids.map((id, i) => {
    let expanded = false;
    const author = new Element('', {href: '/user/profile/' + id});
    const item = new Element('', {'data-comment-id': id});
    item.querySelector = selector => selector.includes('href*=') ? author
      : selector.includes('.name') ? new Element('User ' + i)
      : selector.includes('.content') ? new Element(i === 20 ? 'Doctor Wang Te only at root 21' : 'Neutral content ' + i)
      : selector.includes('.count') ? new Element('0')
      : selector.includes('.date') ? new Element('2026-10-30') : null;
    const root = new Element();
    root.querySelector = selector => selector === '.comment-item' ? item : null;
    const button = new Element('展开更多回复');
    button.click = () => { expanded = true; clickedIds.push(id); };
    root.querySelectorAll = selector => selector.startsWith('button') && !expanded ? [button] : [];
    return root;
  });
  if (reverse) parents.reverse();
  return {ids, clickedIds, parents, Element, context: {HTMLElement: Element,
    document: {
      body: {innerText: '', scrollHeight: 0},
      querySelector: selector => selector.includes('.total') ? new Element('共' + count + '条评论') : null,
      querySelectorAll: selector => selector === '.parent-comment' ? parents : [],
    }, location: {href: 'https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa'},
    window: {innerWidth: 800, innerHeight: 600, scrollTo() {}}, setTimeout(callback, ms) { clock += ms; callback(); },
    Date: class extends Date {static now() { return clock; }}, Math,
  }};
}

// The bridge must resolve without scheduling a page timer. A hidden page may
// never invoke that timer, so an in-page Promise.race timer would not fix it.
const bridgeRead = async (f, progress = {}, extra = {}) => {
  let timer;
  try {
    return await Promise.race([
      vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
        roots: 30, maxReplyClicks: 2, budgetMs: 8000, yieldOnWait: true, progress, ...extra,
      }), f.context),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('Bridge reader waited on a page timer')), 1000); }),
    ]);
  } finally { clearTimeout(timer); }
};
const savedProgress = result => ({threads: Object.fromEntries(result.deepCoverage.threads.map(t => [t.rootKey, t])),
  observedRootIds: result.deepCoverage.observedRootIds,
  ...(result.deepCoverage.rootScrollProgress ? {rootScrollProgress: result.deepCoverage.rootScrollProgress} : {})});

// A reloaded note can fold replies which are already in the cumulative record.
// Restoring that prefix is not discovery and must not block later parent roots.
function restoredPrefixFixture({end = false, pending = false, changedBody = false} = {}) {
  const f = manyRootsFixture(21), root = f.parents[0], rootId = f.ids[0];
  let visibleRoots = 1, expanded = false, scrolls = 0, now = 10000;
  f.context.Date = class extends Date {static now() {return now;}};
  const replyId = 'b'.repeat(24), replyAuthor = 'c'.repeat(24);
  const reply = new f.Element('', {'data-comment-id': replyId});
  reply.querySelector = selector => selector.includes('href*=') ? new f.Element('', {href: '/user/profile/' + replyAuthor})
    : selector.includes('.content') ? new f.Element(changedBody ? 'Actual new verified reply sentence' : 'Already read reply sentence')
    : selector.includes('.name') ? new f.Element('Reply author') : null;
  const button = new f.Element('展开更多回复');
  button.click = () => {expanded = true; f.clickedIds.push(rootId);};
  root.querySelectorAll = selector => selector.includes('.comment-item-sub') ? [reply]
    : selector.startsWith('button') && !expanded ? [button] : [];
  for (const other of f.parents.slice(1)) other.querySelectorAll = () => [];
  const scroller = {tagName: 'DIV', className: 'note-scroller', scrollHeight: 5000, clientHeight: 400, clientWidth: 300,
    getBoundingClientRect: () => ({top: 20, left: 10, width: 300, height: 400, bottom: 420, right: 310}),
    scrollTo() {scrolls++; visibleRoots = 21;}};
  const scope = new f.Element(); scope.contains = element => element === scroller;
  const oldQuery = f.context.document.querySelector;
  f.context.document.querySelector = selector => selector === '.note-scroller' ? scroller
    : selector.includes('#noteContainer') ? scope : oldQuery(selector);
  f.context.document.querySelectorAll = selector => selector === '.parent-comment' ? f.parents.slice(0, visibleRoots)
    : selector.includes('.comments-container .end-container') && end ? [new f.Element('THE END')] : [];
  f.context.window.getComputedStyle = element => ({overflowY: element === scroller ? 'scroll' : 'visible'});
  const body = {commentId: replyId, parentCommentId: rootId, authorId: replyAuthor,
    text: 'Already read reply sentence', textSource: 'rendered_text'};
  const old = {rootKey: 'id:' + rootId, commentId: rootId, state: 'visible_scope_pending_end',
    contentCaptured: true, replyContentCaptured: true, replyIdentityComplete: true,
    replyBodyCaptures: {[replyId]: body}};
  if (pending) old.pendingReplyExpansion = {state: 'click_submitted', clickedAt: 5000,
    beforeReplyIds: [], beforeReplyBodyCaptures: {}, pendingObservations: 1};
  return {...f, rootId, replyId, old, scroller, scope, advance() {now += 10000;}, get scrolls() {return scrolls;}};
}
{
  const f = restoredPrefixFixture({pending: true});
  const r = await bridgeRead(f, {threads: {['id:' + f.rootId]: f.old}, observedRootIds: [f.rootId]}, {maxReplyClicks: 0});
  const thread = r.deepCoverage.threads.find(t => t.commentId === f.rootId);
  assert.notEqual(thread.lastReplyExpansion?.progressBasis, 'new_rendered_reply_id',
    'A previously captured ID reappearing after folding is not new research evidence');
  assert.equal(thread.pendingReplyExpansion.clickedAt, 5000, 'No new global evidence may discard the unresolved old intent');
}
{
  const f = restoredPrefixFixture({pending: true});
  Object.assign(f.old.pendingReplyExpansion, {noteId: 'a'.repeat(24), rootCommentId: f.rootId,
    beforeKnownReplyIds: [f.replyId], beforeKnownReplyBodyCaptures: f.old.replyBodyCaptures});
  const r = await bridgeRead(f, {threads: {['id:' + f.rootId]: f.old}, observedRootIds: [f.rootId]}, {maxReplyClicks: 0});
  const thread = r.deepCoverage.threads.find(t => t.commentId === f.rootId);
  assert.equal(thread.lastReplyExpansion.progressBasis, 'known_reply_render_restored');
  assert.equal(thread.lastReplyExpansion.newResearchEvidence, false);
  assert.deepEqual(Array.from(thread.lastReplyExpansion.restoredReplyIds), [f.replyId]);
  assert.equal(thread.pendingReplyExpansion, undefined, 'Typed observed restoration resolves only this known UI action');
  assert.ok(r.deepCoverage.remainingThreads.some(t => t.commentId === f.rootId), 'Restoring a reply is not scope completion');
}
{
  const f = restoredPrefixFixture({pending: true});
  Object.assign(f.old.pendingReplyExpansion, {noteId: 'a'.repeat(24), rootCommentId: f.rootId,
    beforeKnownReplyIds: [f.replyId], beforeKnownReplyBodyCaptures: {[f.replyId]: {
      ...f.old.replyBodyCaptures[f.replyId], text: '[图片评论]', textSource: 'media_placeholder'}}});
  const r = await bridgeRead(f, {threads: {['id:' + f.rootId]: f.old}, observedRootIds: [f.rootId]}, {maxReplyClicks: 0});
  assert.equal(r.deepCoverage.threads[0].lastReplyExpansion, undefined, 'An unread image/empty cache is not typed restoration body proof');
  assert.equal(r.deepCoverage.threads[0].pendingReplyExpansion.clickedAt, 5000);
}
for (const text of ['原评论已删除', '该评论已删除', '此评论已被删除。', '评论已被删除!', ' 原 评论 已 删除 ！']) {
  const f = restoredPrefixFixture({pending: true});
  const capturedText = text.trim();
  f.old.replyBodyCaptures[f.replyId].text = capturedText;
  const reply = f.parents[0].querySelectorAll('.reply-container .comment-item-sub')[0], oldQuery = reply.querySelector;
  reply.querySelector = selector => selector.includes('.content') ? new f.Element(text) : oldQuery(selector);
  Object.assign(f.old.pendingReplyExpansion, {noteId: 'a'.repeat(24), rootCommentId: f.rootId,
    beforeKnownReplyIds: [f.replyId], beforeKnownReplyBodyCaptures: f.old.replyBodyCaptures});
  const r = await bridgeRead(f, {threads: {['id:' + f.rootId]: f.old}, observedRootIds: [f.rootId]}, {maxReplyClicks: 0});
  const thread = r.deepCoverage.threads[0];
  assert.equal(thread.lastReplyExpansion, undefined, 'An exact deletion placeholder is not recovery proof: ' + text);
  assert.equal(thread.pendingReplyExpansion.clickedAt, 5000);
  assert.ok(r.results.some(row => row.commentId === f.replyId && row.text === capturedText), 'The deletion ID and actual text remain captured');
  assert.equal(thread.replyBodyCaptures[f.replyId].text, capturedText, 'Tombstones are preserved, never erased to manufacture progress');
}
for (const text of ['我之前的评论已被删除，现在补充治疗经历。', '仍有可读治疗内容']) {
  const f = restoredPrefixFixture({pending: true});
  f.old.replyBodyCaptures[f.replyId].text = text;
  const reply = f.parents[0].querySelectorAll('.reply-container .comment-item-sub')[0], oldQuery = reply.querySelector;
  reply.querySelector = selector => selector.includes('.content') ? new f.Element(text)
    : selector.includes('.name') ? new f.Element('用户已注销') : oldQuery(selector);
  Object.assign(f.old.pendingReplyExpansion, {noteId: 'a'.repeat(24), rootCommentId: f.rootId,
    beforeKnownReplyIds: [f.replyId], beforeKnownReplyBodyCaptures: f.old.replyBodyCaptures});
  const r = await bridgeRead(f, {threads: {['id:' + f.rootId]: f.old}, observedRootIds: [f.rootId]}, {maxReplyClicks: 0});
  assert.equal(r.deepCoverage.threads[0].lastReplyExpansion.progressBasis, 'known_reply_render_restored');
  assert.equal(r.deepCoverage.threads[0].lastReplyExpansion.newResearchEvidence, false);
  assert.ok(r.results.some(row => row.commentId === f.replyId && row.author === '用户已注销' && row.text === text));
}
{
  const f = restoredPrefixFixture();
  let r = await bridgeRead(f, {threads: {['id:' + f.rootId]: f.old}, observedRootIds: [f.rootId]});
  assert.equal(f.clickedIds.length, 0, 'Restoring already-read replies must yield to parent discovery before END');
  assert.equal(f.scrolls, 1, 'A resumable known prefix must submit a bounded parent scroll');
  f.advance(); r = await bridgeRead(f, savedProgress(r));
  assert.ok(r.results.some(row => row.commentId === f.ids[20] && row.text.includes('root 21')));
  assert.equal(r.deepCoverage.explicitEnd, false);
  assert.notEqual(r.deepCoverage.rootPaginationState, 'complete');
  assert.ok(r.deepCoverage.remainingThreads.some(t => t.commentId === f.rootId), 'Deferred replies remain a real gap');
}
{
  const f = restoredPrefixFixture(), oldQuery = f.parents[0].querySelectorAll;
  f.parents[0].querySelectorAll = selector => selector.startsWith('button') ? [] : oldQuery(selector);
  const r = await bridgeRead(f, {threads: {['id:' + f.rootId]: f.old}, observedRootIds: [f.rootId]});
  assert.equal(f.scrolls, 1, 'Unknown reply totals without a current safe control may still discover parent roots');
  assert.ok(r.deepCoverage.remainingThreads.some(t => t.commentId === f.rootId));
  assert.equal(r.deepCoverage.explicitEnd, false, 'Physical scroll or an absent reply control never creates END');
}
{
  const f = restoredPrefixFixture({end: true});
  const r = await bridgeRead(f, {threads: {['id:' + f.rootId]: f.old}, observedRootIds: [f.rootId]});
  assert.equal(f.clickedIds.length, 1, 'A real parent END returns the known pending floor to reply processing');
  assert.equal(r.deepCoverage.threads[0].pendingReplyExpansion.state, 'click_submitted');
  assert.equal(r.deepCoverage.threads[0].pendingReplyExpansion.beforeKnownReplyIds.includes(f.replyId), true);
  assert.ok(r.deepCoverage.remainingThreads.length, 'A parent END does not finish an unknown reply click');
}
{
  const f = restoredPrefixFixture({pending: true, changedBody: true});
  const r = await bridgeRead(f, {threads: {['id:' + f.rootId]: f.old}, observedRootIds: [f.rootId]}, {maxReplyClicks: 0});
  assert.equal(r.deepCoverage.threads[0].lastReplyExpansion.progressBasis, 'changed_verified_reply_body',
    'An actual same-ID/same-author/same-parent body change still permits real progress');
}
{
  const f = restoredPrefixFixture({pending: true});
  f.parents[0].querySelectorAll('.reply-container .comment-item-sub')[0].attributes['data-comment-id'] = 'd'.repeat(24);
  const r = await bridgeRead(f, {threads: {['id:' + f.rootId]: f.old}, observedRootIds: [f.rootId]}, {maxReplyClicks: 0});
  assert.equal(r.deepCoverage.threads[0].lastReplyExpansion.progressBasis, 'new_rendered_reply_id');
  assert.equal(r.deepCoverage.threads[0].lastReplyExpansion.newResearchEvidence, true);
}

// Counter placeholders and asynchronous empty loads must never prove the end.
const discoveredReplyScope = rootIds => ({kind: 'discovered_replies_v1', noteId: 'a'.repeat(24),
  scopeId: 'd'.repeat(64), taskId: 'native-' + 'e'.repeat(20), phase: 'probe', rootIds, maxRootScrolls: 0});
{
  const f = manyRootsFixture(2);
  const r = await bridgeRead(f, {observedRootIds: f.ids,
    rootScrollProgress: {state: 'stalled', attempts: 3, stalledRounds: 3},
    discoveredReplyScope: discoveredReplyScope([f.ids[0]])});
  assert.deepEqual(f.clickedIds, [f.ids[0]], 'A root-pagination fault cannot starve a known parent reply control');
  assert.ok(!r.results.some(row => row.commentId === f.ids[1]), 'Unknown/out-of-scope parent content is not read by reply-only collection');
  assert.equal(r.deepCoverage.scrollRounds, 0);
  assert.equal(r.deepCoverage.rootScrollProgress.attempts, 3, 'Reply-only mode never retries or resets root scrolling');
  assert.equal(r.deepCoverage.rootPaginationState, 'stalled');
  assert.equal(r.deepCoverage.discoveredReplyScope.maxRootScrolls, 0);
}
{
  const f = restoredPrefixFixture();
  const r = await bridgeRead(f, {threads: {['id:' + f.rootId]: f.old}, observedRootIds: [f.rootId],
    rootScrollProgress: {state: 'stalled', attempts: 3, stalledRounds: 3},
    discoveredReplyScope: discoveredReplyScope([f.rootId])});
  assert.equal(f.clickedIds.length, 1, 'Reply phase returns cached folded known roots to expansion despite missing parent END');
  assert.equal(f.scrolls, 0);
  assert.equal(r.deepCoverage.rootPaginationState, 'stalled');
  assert.ok(r.deepCoverage.remainingThreads.length, 'Reply work cannot turn a parent fault into full completion');
}
{
  const f = manyRootsFixture(2);let now = 10000;
  f.context.Date = class extends Date {static now() {return now;}};
  const scope = discoveredReplyScope(f.ids);
  let r = await bridgeRead(f, {observedRootIds: f.ids,
    rootScrollProgress: {state: 'stalled', attempts: 3, stalledRounds: 3}, discoveredReplyScope: scope});
  const firstIntent = r.deepCoverage.threads[0].pendingReplyExpansion.clickedAt;
  const progress = savedProgress(r);
  progress.threads['id:' + f.ids[0]].discoveredReplyVisit = {scopeId: scope.scopeId,
    rootCommentId: f.ids[0], state: 'captured_visible_scope', semanticReviewed: false};
  now += 10000; r = await bridgeRead(f, {...progress, discoveredReplyScope: scope});
  assert.deepEqual(f.clickedIds, f.ids, 'An unknown first reply click cannot starve the second known floor during a two-observation probe');
  assert.equal(r.deepCoverage.threads[0].pendingReplyExpansion.clickedAt, firstIntent);
  assert.equal(r.deepCoverage.scrollRounds, 0);
  assert.equal(r.deepCoverage.rootScrollProgress.attempts, 3);
  assert.ok(!r.deepCoverage.discoveredReplyScope.capturedVisibleRootIds.includes(f.ids[0]), 'An old visit cannot hide a current unknown action');
}
{
  const f = manyRootsFixture(1); f.parents[0].querySelectorAll = () => [];
  const r = await bridgeRead(f, {observedRootIds: f.ids,
    rootScrollProgress: {state: 'stalled', attempts: 3, stalledRounds: 3},
    discoveredReplyScope: discoveredReplyScope(f.ids)});
  assert.equal(r.deepCoverage.discoveredReplyScope.state, 'known_parent_visit_pass_finished');
  assert.equal(r.deepCoverage.discoveredReplyScope.capturedVisibleRootIds.length, 1);
  assert.equal(r.deepCoverage.discoveredReplyScope.unvisitedRootIds.length, 0);
  assert.equal(r.deepCoverage.explicitEnd, false);
  assert.equal(r.deepCoverage.scrollRounds, 0);
  assert.ok(r.deepCoverage.remainingThreads.length);
}
{
  const f = manyRootsFixture(1), scope = discoveredReplyScope(f.ids); scope.noteId = 'f'.repeat(24);
  await assert.rejects(bridgeRead(f, {discoveredReplyScope: scope}), /DISCOVERED_REPLY_SCOPE_INVALID/);
  assert.equal(f.clickedIds.length, 0);
}
{
  // Each ten-unit doctor rotation creates a fresh folded document. A visit is
  // an observed capture fact only: it must reach root21's genuinely unread B.
  const f = manyRootsFixture(25), visible = f.ids.map(() => 1), clicked = [], cache = {};
  let now = 10000, capturedNewB = false, last;
  f.context.Date = class extends Date {static now() {return now;}};
  f.context.performance = {timeOrigin: 9000};
  f.parents.forEach((parent, i) => {
    const bodies = {};
    const children = [0, 1].map(j => {
      const cid = (10000 + i * 2 + j).toString(16).padStart(24, '0');
      const text = i === 20 && j === 1 ? 'Genuine unread doctor attribution in root21 reply B' : 'Known reply ' + i + '/' + j;
      const child = new f.Element('', {'data-comment-id': cid});
      child.querySelector = selector => selector.includes('href*=') ? new f.Element('', {href: '/user/profile/' + '9'.repeat(24)})
        : selector.includes('.name') ? new f.Element('Reply author')
        : selector.includes('.content') ? new f.Element(text) : null;
      if (i !== 20 || j !== 1) bodies[cid] = {commentId: cid, parentCommentId: f.ids[i],
        authorId: '9'.repeat(24), text, textSource: 'rendered_text'};
      return child;
    });
    const button = new f.Element('展开 1 条回复');
    button.click = () => {visible[i] = 2; clicked.push(i + 1);};
    parent.querySelectorAll = selector => selector.includes('.comment-item-sub') ? children.slice(0, visible[i])
      : selector.startsWith('button') && visible[i] < 2 ? [button] : [];
    cache['id:' + f.ids[i]] = {rootKey: 'id:' + f.ids[i], commentId: f.ids[i], state: 'visible_scope_pending_end',
      contentCaptured: true, replyContentCaptured: true, replyIdentityComplete: true, replyBodyCaptures: bodies};
  });
  let progress = {threads: cache, observedRootIds: f.ids,
    rootScrollProgress: {state: 'stalled', attempts: 8, stalledRounds: 3}};
  for (let doc = 0; doc < 3; doc++) {
    visible.fill(1); f.context.performance.timeOrigin = now - 1;
    for (let unit = 0; unit < 10; unit++) {
      now += 10000; last = await bridgeRead(f, {...progress, discoveredReplyScope: discoveredReplyScope(f.ids)});
      capturedNewB ||= last.results.some(row => row.text.includes('Genuine unread doctor attribution'));
      progress = savedProgress(last);
    }
  }
  assert.ok(clicked.includes(21) && capturedNewB, 'Fresh documents cannot starve the real unread reply at floor21');
  assert.equal(new Set(clicked).size, 25);
  assert.equal(clicked.length, 25, 'The captured prefix does not consume every later document again');
  assert.equal(last.deepCoverage.discoveredReplyScope.state, 'known_parent_visit_pass_finished');
  assert.equal(last.deepCoverage.discoveredReplyScope.capturedVisibleRootIds.length, 25);
  assert.equal(last.deepCoverage.rootScrollProgress.attempts, 8);
  assert.equal(last.deepCoverage.explicitEnd, false);
  assert.equal(last.deepCoverage.rootPaginationState, 'stalled');
  assert.equal(last.deepCoverage.remainingThreads.length, 25, 'A completed capture pass is still an unfinished global reply scope');
  assert.ok(last.deepCoverage.threads.every(thread => thread.discoveredReplyVisit.semanticReviewed === false));
}

function emptyCommentFixture(options = {}) {
  const f = manyRootsFixture(0);
  const state = {now: 10000, header: options.header ?? '共0条评论', bar: options.bar ?? '评论',
    emptyText: options.emptyText || '', loading: !!options.loading, end: !!options.end, parents: [], scrolls: 0};
  const container = new f.Element();
  const empty = new f.Element();
  const loading = new f.Element('加载中');
  container.contains = element => element === empty;
  container.querySelectorAll = () => state.loading ? [loading] : [];
  empty.getClientRects = () => options.hidden ? [] : [{}];
  empty.closest = () => options.inComment ? new f.Element() : null;
  const document = f.context.document;
  document.body.innerText = options.bodyText || '';
  document.querySelector = selector => selector === '.comments-container .total' ? new f.Element(state.header)
    : selector === '.interact-container .chat-wrapper .count' ? new f.Element(state.bar)
    : selector === '.comments-container' ? container : null;
  document.querySelectorAll = selector => selector === '.parent-comment' ? state.parents
    : selector.includes('.comments-container .end-container') && state.end ? [new f.Element('- THE END -')]
    : selector.includes('.comments-container .empty-text') && state.emptyText ? (empty.textContent = state.emptyText, [empty]) : [];
  f.context.window.scrollTo = () => {state.scrolls++;};
  f.context.Date = class extends Date {static now() {return state.now;}};
  f.context.setTimeout = () => {throw new Error('An empty comment batch must yield instead of leaving a timer');};
  return {...f, state};
}
function noCommentsComponentFixture(options = {}) {
  const f = emptyCommentFixture({...options, header: options.header ?? ''});
  const scroller = new f.Element(), owner = new f.Element(), box = new f.Element(), text = new f.Element();
  const mark=(element,tag,classes)=>{
    element.tagName=tag;element.classList={contains:name=>classes.split(' ').includes(name)};
    element.getAttribute=name=>name==='aria-busy' && element.busy?'true':null;
    element.matches=selector=>selector==='[aria-busy="true"]'?element.getAttribute('aria-busy')==='true'
      :selector.startsWith('.') && element.classList.contains(selector.slice(1));
    element.getClientRects=()=>element.hidden?[]:[{}];return element;
  };
  mark(scroller,'DIV','note-scroller'+(options.scrollerLoadingClass?' loading':'')+(options.scrollerLoadingContainer?' loading-container':''));
  mark(owner,'DIV',(options.wrongOwner?'wrong-owner':'comments-el')+(options.ownerLoadingClass?' loading':''));
  mark(box,'DIV',(options.wrongBox?'wrong-box':'no-comments')+(options.boxLoadingClass?' loading-container':''));
  mark(text,options.wrongTag?'SPAN':'P','no-comments-text');
  scroller.busy=!!options.scrollerBusy;owner.busy=!!options.ownerBusy;box.busy=!!options.boxBusy;
  owner.parentElement=scroller;box.parentElement=owner;text.parentElement=box;
  if(options.wrongAncestor)owner.parentElement=new f.Element();
  text.textContent=options.emptyText ?? '这是一片荒地\n点击评论';
  text.closest=()=>options.inComment||options.inBody?new f.Element():null;
  if(options.hidden)text.hidden=true;if(options.hiddenOwner)owner.hidden=true;if(options.hiddenScroller)scroller.hidden=true;
  const orphan=mark(new f.Element('quoted'),'DIV','comment-item');
  scroller.querySelectorAll=selector=>selector===':scope > .comments-el > .no-comments > p.no-comments-text'
    ? options.absent?[]:options.ambiguous?[text,text]:[text]
    : selector.includes('.loading') ? [owner,box,text].filter(el=>selector.split(',').some(s=>el.matches(s))).concat(f.state.loading?[new f.Element('loading')]:[])
    : selector.includes('.parent-comment') && options.orphanComment?[orphan]:[];
  const detail=new f.Element();detail.querySelector=selector=>selector==='.note-scroller'?scroller:null;
  const original=f.context.document.querySelector;
  f.context.document.querySelector=selector=>selector==='.comments-container'?options.containerPresent?original(selector):null
    :selector==='.note-detail-mask,.note-detail-container,.note-container'?detail
    :selector==='.comments-container .total'?!options.header?null:original(selector):original(selector);
  return f;
}
for(const text of ['这是一片荒地 点击评论','这是一片荒地\n点击评论','这是一片荒地点击评论']) {
  const f=noCommentsComponentFixture({emptyText:text}),r=await bridgeRead(f);
  assert.equal(r.deepCoverage.rootPaginationState,'complete');assert.equal(r.deepCoverage.explicitEnd,false);
  assert.equal(r.deepCoverage.rootEndBasis,'rendered_comment_empty_state');
  assert.equal(r.deepCoverage.emptyCommentState.variant,'no_comments_component');
  assert.equal(r.deepCoverage.emptyCommentState.commentsContainerPresent,false);
  assert.equal(r.deepCoverage.emptyCommentState.directParentChainVerified,true);
  assert.equal(r.deepCoverage.emptyCommentState.acceptedAsEnd,true);assert.equal(f.state.scrolls,0);
}
for(const options of [
  {inComment:true},{inBody:true},{hidden:true},{hiddenOwner:true},{hiddenScroller:true},
  {loading:true},{header:'共1条评论'},{bar:'5'},{wrongOwner:true},{wrongBox:true},{wrongTag:true},
  {scrollerLoadingClass:true},{scrollerLoadingContainer:true},{scrollerBusy:true},
  {ownerLoadingClass:true},{ownerBusy:true},{boxLoadingClass:true},{boxBusy:true},
  {wrongAncestor:true},{orphanComment:true},{ambiguous:true},{absent:true},{containerPresent:true},
  {emptyText:'这是一片荒地'},{emptyText:'点击评论'},
  {emptyText:'正文说这是一片荒地 点击评论'},{emptyText:'这是一片荒地 点击评论 私信我'},
]) {
  const r=await bridgeRead(noCommentsComponentFixture(options));
  assert.notEqual(r.deepCoverage.rootPaginationState,'complete',JSON.stringify(options));
  assert.notEqual(r.deepCoverage.emptyCommentState?.acceptedAsEnd,true);
}
{
  const r=await bridgeRead(noCommentsComponentFixture(),{observedRootIds:['b'.repeat(24)]});
  assert.notEqual(r.deepCoverage.rootPaginationState,'complete','A no-comments component cannot erase prior observed parents');
}
{
  const f=noCommentsComponentFixture(),one=manyRootsFixture(1);one.parents[0].querySelectorAll=()=>[];f.state.parents=one.parents;
  const r=await bridgeRead(f);assert.equal(r.results.length,1);
  assert.notEqual(r.deepCoverage.rootPaginationState,'complete','Actual parents contradict the standalone empty UI');
}
for (const options of [{}, {header: '', bar: '评论'}, {header: '共0条评论', bar: '0'}, {header: '', bar: ''}]) {
  const f = emptyCommentFixture(options), r = await bridgeRead(f);
  assert.notEqual(r.deepCoverage.rootPaginationState, 'complete', 'Zero/unknown counters alone cannot complete an empty snapshot');
  assert.equal(r.deepCoverage.explicitEnd, false);
  assert.equal(r.deepCoverage.rootEndBasis, null);
  assert.equal(r.deepCoverage.emptyCommentState, null);
  assert.equal(r.deepCoverage.commentCountObservation.latest.zeroIsEndSignal, false);
  assert.equal(r.deepCoverage.commentCountObservation.latest.header.text, options.header ?? '共0条评论');
  assert.equal(r.deepCoverage.commentCountObservation.latest.interactionBar.text, options.bar ?? '评论');
}
{
  const f = emptyCommentFixture({emptyText: '还没有评论，快来抢沙发吧'}), r = await bridgeRead(f);
  assert.equal(r.deepCoverage.rootPaginationState, 'complete');
  assert.equal(r.deepCoverage.explicitEnd, false, 'A real empty state is recorded separately from a THE END signal');
  assert.equal(r.deepCoverage.rootEndBasis, 'rendered_comment_empty_state');
  assert.equal(r.deepCoverage.emptyCommentState.text, '还没有评论，快来抢沙发吧');
  assert.equal(r.deepCoverage.emptyCommentState.acceptedAsEnd, true);
  assert.equal(f.state.scrolls, 0);
}
for (const options of [
  {emptyText: '暂无评论', hidden: true}, {emptyText: '暂无评论', inComment: true},
  {emptyText: '暂无评论', loading: true}, {emptyText: '暂无评论', header: '共5条评论', bar: '5'},
  {bodyText: '暂无评论'},
]) {
  const f = emptyCommentFixture(options), r = await bridgeRead(f);
  assert.notEqual(r.deepCoverage.rootPaginationState, 'complete', 'Hidden, quoted, loading or contradicted empty text is not an end');
  assert.notEqual(r.deepCoverage.emptyCommentState?.acceptedAsEnd, true);
}
for (const options of [
  {header: '共0条评论', bar: '5'}, {header: '暂无评论', bar: '5'},
  {header: '共0条评论', bar: '1.2万'}, {header: '共5条评论', bar: '0'},
]) {
  const f = emptyCommentFixture({...options, emptyText: '暂无评论'}), r = await bridgeRead(f);
  assert.notEqual(r.deepCoverage.rootPaginationState, 'complete', 'Either visible positive source contradicts an empty state even when the other parser has precedence');
  assert.equal(r.deepCoverage.emptyCommentState.acceptedAsEnd, false);
  assert.equal(r.deepCoverage.commentCountObservation.latest.positiveTotalObserved, true);
  assert.ok(r.deepCoverage.commentCountObservation.latest.headerParsedTotal > 0 ||
    r.deepCoverage.commentCountObservation.latest.interactionBarParsedTotal > 0);
}
{
  const f = emptyCommentFixture({emptyText: '暂无评论'}), priorId = 'b'.repeat(24);
  const r = await bridgeRead(f, {observedRootIds: [priorId]});
  assert.notEqual(r.deepCoverage.rootPaginationState, 'complete', 'A small empty snapshot cannot erase an already discovered parent');
  assert.equal(r.deepCoverage.emptyCommentState.acceptedAsEnd, false);
}
{
  const f = emptyCommentFixture({emptyText: '暂无评论'}), one = manyRootsFixture(1);
  one.parents[0].querySelectorAll = () => [];f.state.parents = one.parents;
  const r = await bridgeRead(f);
  assert.equal(r.results.length, 1);
  assert.notEqual(r.deepCoverage.rootPaginationState, 'complete', 'Actual parents contradict an empty-state shell');
  assert.equal(r.deepCoverage.emptyCommentState.acceptedAsEnd, false);
}
{
  const f = emptyCommentFixture(), first = await bridgeRead(f), one = manyRootsFixture(1);
  one.parents[0].querySelectorAll = () => [];
  f.state.now += 10000;f.state.header = '共1条评论';f.state.bar = '1';f.state.parents = one.parents;
  const later = await bridgeRead(f, savedProgress(first));
  assert.equal(later.results.length, 1, 'A zero placeholder batch must leave room for asynchronously arriving parents');
  assert.notEqual(later.deepCoverage.rootPaginationState, 'complete');
}
{
  const f = emptyCommentFixture({end: true}), r = await bridgeRead(f);
  assert.equal(r.deepCoverage.rootPaginationState, 'complete');
  assert.equal(r.deepCoverage.rootEndBasis, 'rendered_end_signal');
  assert.equal(r.deepCoverage.explicitEnd, true);
}
{
  const f = emptyCommentFixture(), r = await bridgeRead(f, {apiRootsComplete: true, expectedRootIds: []});
  assert.equal(r.deepCoverage.rootPaginationState, 'complete', 'An independently complete API root list remains a supported end source');
  assert.equal(r.deepCoverage.rootEndBasis, 'complete_api_root_list');
}
{
  const f = emptyCommentFixture(), r = await bridgeRead(f, {rootPaginationState: 'complete', status: 'visible_scope_complete'});
  assert.notEqual(r.deepCoverage.rootPaginationState, 'complete', 'A legacy unproven derived completion is not fresh end evidence');
  assert.equal(r.deepCoverage.rootEndBasis, null);
}
process.stdout.write('Zero-count/end evidence checks passed: placeholders, async arrival, rendered empty state and contradictory sources.\n');
{
  const f = manyRootsFixture(2);let now = 10000, progress = {}, last;
  f.context.Date = class extends Date {static now() {return now;}};
  f.context.setTimeout = () => {throw new Error('Pending diagnostics must not leave a timer');};
  for (let batch = 0; batch < 8; batch++) {
    now += 10000;last = await bridgeRead(f, progress, {roots: 1});progress = savedProgress(last);
  }
  assert.equal(f.clickedIds.filter(id => id === f.ids[0]).length, 1, 'An old unknown click must never be replayed');
  assert.ok(f.clickedIds.includes(f.ids[1]), 'An isolated first floor must not consume every one-root batch');
  const first = last.deepCoverage.threads.find(thread => thread.commentId === f.ids[0]);
  assert.equal(first.pendingReplyExpansion.state, 'outcome_unconfirmed');
  assert.equal(first.pendingReplyExpansion.originalSubmissionState, 'click_submitted');
  assert.ok(first.pendingReplyExpansion.pendingObservations >= 3);
  assert.equal(first.pendingReplyExpansion.diagnostic.retryPermitted, false);
  assert.equal(first.state, 'stalled');
  assert.ok(last.deepCoverage.remainingThreads.some(thread => thread.commentId === f.ids[0]));
}
{
  const f = manyRootsFixture(21);let now = 10000, progress = {}, last;
  for (let index = 1; index < 20; index++) f.parents[index].querySelectorAll = () => [];
  f.context.Date = class extends Date {static now() {return now;}};
  f.context.setTimeout = () => {throw new Error('Later floors must not depend on a page timer');};
  for (let batch = 0; batch < 5 && !f.clickedIds.includes(f.ids[20]); batch++) {
    now += 10000;last = await bridgeRead(f, progress);progress = savedProgress(last);
  }
  assert.equal(f.clickedIds.filter(id => id === f.ids[0]).length, 1);
  assert.ok(f.clickedIds.includes(f.ids[20]), 'A pending first floor must not starve the doctor name at floor21');
  assert.ok(last.results.some(row => row.commentId === f.ids[20] && row.text.includes('root 21')));
  assert.ok(last.deepCoverage.remainingThreads.some(thread => thread.commentId === f.ids[0]));
}
{
  const f = fixture(false, {advancingClock: true, neverCallback: true, deferReplies: true, realIds: true});
  const first = await bridgeRead(f);
  assert.equal(f.state.timerCalls, 0, 'Bridge waits must not create page timers');
  assert.equal(f.state.clicks, 1);
  assert.ok(first.results.length, 'Already captured root survives the handoff');
  assert.equal(first.deepCoverage.rootPaginationState, 'pending');
  assert.equal(first.deepCoverage.checkpoint.reason, 'reply_render_pending');
  assert.equal(first.deepCoverage.waitHandedToScheduler, true);
  assert.equal(first.deepCoverage.threads[0].pendingReplyExpansion.state, 'click_submitted');
  let progress = savedProgress(first);
  for (const delay of [500, 86400000]) {
    f.state.now += delay;
    const waiting = await bridgeRead(f, progress);
    assert.equal(f.state.clicks, 1, 'An unresolved submitted click must not be repeated in another batch');
    assert.equal(waiting.deepCoverage.rootPaginationState, 'pending');
    assert.equal(waiting.deepCoverage.threads[0].pendingReplyExpansion.clickedAt, 10000);
    progress = savedProgress(waiting);
  }
  f.state.replies = 1; // Real DOM progress becomes visible between outer batches.
  f.state.now += 500; // Settle any bounded root scroll submitted while the old click was isolated.
  const advanced = await bridgeRead(f, progress);
  assert.equal(f.state.clicks, 2, 'A further reply page is allowed only after actual DOM progress');
  assert.equal(advanced.results.filter(row => row.is_reply).length, 1);
  assert.equal(advanced.deepCoverage.threads[0].lastReplyExpansion.observedVisibleReplies, 1);
  assert.equal(f.state.timerCalls, 0);
}
{
  let body = 'Old verified reply body';
  const f = fixture(false, {advancingClock: true, neverCallback: true, deferReplies: true, realIds: true,
    replyText: () => body});
  f.state.replies = 1;
  let r = await bridgeRead(f), progress = savedProgress(r);
  const clickedAt = r.deepCoverage.threads[0].pendingReplyExpansion.clickedAt;
  for (let batch = 0; batch < 3; batch++) {
    f.state.now += 10000;r = await bridgeRead(f, progress);progress = savedProgress(r);
  }
  assert.equal(f.state.clicks, 1);
  assert.equal(r.deepCoverage.threads[0].pendingReplyExpansion.state, 'outcome_unconfirmed');
  body = 'Late verified negative sentence about the same doctor';f.state.now += 10000;
  r = await bridgeRead(f, progress);
  assert.equal(r.deepCoverage.threads[0].lastReplyExpansion.clickedAt, clickedAt);
  assert.equal(r.deepCoverage.threads[0].lastReplyExpansion.progressBasis, 'changed_verified_reply_body');
  assert.ok(r.results.some(row => row.is_reply && row.text === body));
  assert.equal(f.state.clicks, 2, 'Only an actually changed body under the same real reply/author/parent identity permits further expansion');
}
{
  const f = fixture(false, {advancingClock: true, neverCallback: true, realIds: true});
  const first = await bridgeRead(f);
  f.state.now += 1000;
  const paced = await bridgeRead(f, savedProgress(first));
  assert.equal(f.state.clicks, 1, 'Cross-batch three-second click spacing remains enforced');
  assert.equal(paced.deepCoverage.checkpoint.reason, 'reply_click_pacing_pending');
  f.state.now += 2000;
  await bridgeRead(f, savedProgress(paced));
  assert.equal(f.state.clicks, 2);
  assert.equal(f.state.clickTimes[1] - f.state.clickTimes[0], 3000);
}
{
  // A total budget is checked inside root/reply scans. Resuming from merged
  // snapshots reaches all 245 roots, including the doctor's late root 21.
  const f = manyRootsFixture(245);
  let now = 10000, progress = {threads: {}, observedRootIds: []};
  const captured = new Map();
  f.context.Date = class extends Date {static now() {return now;}};
  f.context.setTimeout = () => { throw new Error('Page timer must not be scheduled'); };
  for (const parent of f.parents) {
    parent.querySelectorAll = () => [];
    const item = parent.querySelector('.comment-item'), query = item.querySelector;
    item.querySelector = selector => {
      if (selector.includes('.content')) now += 20;
      return query(selector);
    };
  }
  for (let batch = 0; batch < 30 && captured.size < 245; batch++) {
    const result = await bridgeRead(f, progress, {roots: 200, budgetMs: 1000});
    assert.equal(result.deepCoverage.rootPaginationState, 'pending', 'A budget stop is never a platform end');
    assert.equal(result.deepCoverage.timeBudgetReached, true);
    assert.ok(result.deepCoverage.elapsedMs <= 1080, 'A single DOM item may finish; no unbounded post-budget rescan');
    assert.ok(result.deepCoverage.rootsProcessed > 0, 'Complete non-expandable roots advance during a bounded scan');
    for (const row of result.results) captured.set(row.commentId, row);
    progress = {threads: {...progress.threads, ...savedProgress(result).threads},
      observedRootIds: [...new Set([...progress.observedRootIds, ...result.deepCoverage.observedRootIds])]};
  }
  assert.equal(captured.size, 245, 'Smaller bounded snapshots accumulate without losing earlier roots');
  assert.ok([...captured.values()].some(row => row.text.includes('root 21')));
  assert.equal(Object.keys(progress.threads).length, 245);
}
{
  const f = manyRootsFixture(1), parent = f.parents[0];
  let now = 10000;
  const replies = Array.from({length: 30}, (_, i) => {
    const reply = new f.Element('', {'data-comment-id': (1000 + i).toString(16).padStart(24, '0')});
    reply.querySelector = selector => {
      if (selector.includes('href*=')) return new f.Element('', {href: '/user/profile/' + 'b'.repeat(24)});
      if (selector.includes('.nickname')) return null;
      if (selector.includes('.content')) {now += 100; return new f.Element('Reply body ' + i);}
      if (selector.includes('.name')) return new f.Element('Reply author');
      return null;
    };
    return reply;
  });
  parent.querySelectorAll = selector => selector.includes('.comment-item-sub') ? replies : [];
  f.context.Date = class extends Date {static now() {return now;}};
  const result = await bridgeRead(f, {}, {budgetMs: 1000});
  assert.ok(result.results.some(row => row.is_reply), 'A partial reply scan keeps captured bodies');
  assert.ok(result.results.filter(row => row.is_reply).length < 30, 'Reply scanning is bounded');
  assert.equal(result.deepCoverage.threads[0].scanIncomplete, true);
  assert.equal(result.deepCoverage.threads[0].replyContentCaptured, false);
  assert.equal(result.deepCoverage.threads[0].state, 'pending');
  assert.equal(result.deepCoverage.rootPaginationState, 'pending');
}
{
  const blocked = fixture(true, {neverCallback: true});
  await assert.rejects(bridgeRead(blocked), /XHS_ACCESS_RESTRICTED/);
  assert.equal(blocked.state.clicks, 0);
  const mismatch = fixture(false, {neverCallback: true});
  await assert.rejects(bridgeRead(mismatch, {}, {accountUid: 'b'.repeat(24)}), /ACCOUNT_MISMATCH/);
  assert.equal(mismatch.state.clicks, 0);
  const changed = fixture(false, {navigateOnExpand: true, rejectPrivateRead: true, neverCallback: true});
  const result = await bridgeRead(changed);
  assert.equal(result.scopeChanged, true, 'Checkpoint mode retains the exact note-scope guard');
  assert.ok(result.partial.results.length);
}

function rootPagingFixture() {
  const f = manyRootsFixture(2);
  const state = {now: 10000, scrolls: 0, visible: 1, end: false};
  for (const parent of f.parents) parent.querySelectorAll = () => [];
  const container = {tagName: 'DIV', className: 'comments-container', scrollTop: 0,
    scrollHeight: 1000, clientHeight: 400, clientWidth: 300, parentElement: null,
    getBoundingClientRect: () => ({top: 20, left: 10, width: 300, height: 400, bottom: 420, right: 310}),
    scrollTo(x, y) {state.scrolls++;this.scrollTop = Math.min(y, this.scrollHeight - this.clientHeight);}};
  const oldQuery = f.context.document.querySelector;
  f.context.document.querySelector = selector => selector === '.comments-container' ? container : oldQuery(selector);
  f.context.document.querySelectorAll = selector => selector === '.parent-comment' ? f.parents.slice(0, state.visible)
    : selector.includes('.comments-container .end-container') && state.end ? [new f.Element('- THE END -')] : [];
  f.context.window.getComputedStyle = () => ({overflowY: 'auto'});
  f.context.window.innerWidth = 800;f.context.window.innerHeight = 600;
  f.context.Date = class extends Date {static now() {return state.now;}};
  f.context.setTimeout = () => {throw new Error('Root pagination must not schedule a page timer');};
  return {...f, state, container};
}
function noParentScrollFixture(options={}) {
  const f=emptyCommentFixture(),state=f.state;state.sideScrolls=0;state.documentScrolls=0;
  const side={tagName:'DIV',className:'note-scroller',scrollHeight:2000,clientHeight:options.zeroHeight?0:400,
    clientWidth:300,scrollTop:0,getBoundingClientRect:()=>({top:20,left:10,width:300,height:400,bottom:420,right:310}),
    scrollTo(x,y){state.sideScrolls++;this.scrollTop=Math.min(y,1600);}};
  const scope=new f.Element();scope.contains=element=>!options.outsideScope&&(element===side||element===comments);
  const html={tagName:'HTML',className:'',scrollHeight:600,clientHeight:600,clientWidth:800,scrollTop:0,
    contains:element=>element===scope,getBoundingClientRect:()=>({top:0,left:0,width:800,height:600,bottom:600,right:800}),
    scrollTo(){state.documentScrolls++;}};
  const comments=Object.assign(new f.Element(),{tagName:'DIV',className:'comments-container',scrollHeight:100,
    clientHeight:100,clientWidth:300,parentElement:side,contains:()=>false,
    getBoundingClientRect:()=>({top:20,left:10,width:300,height:100,bottom:120,right:310})});
  const old=f.context.document.querySelector,oldAll=f.context.document.querySelectorAll;
  f.context.document.querySelector=selector=>selector.includes('#noteContainer')?scope
    :selector==='.comments-container'?(options.commentsAncestor?comments:null)
    :selector==='.note-scroller'?side:old(selector);
  f.context.document.querySelectorAll=selector=>selector==='.note-scroller'?[side]:oldAll(selector);
  f.context.document.scrollingElement=html;
  f.context.window.getComputedStyle=element=>({overflowY:element===side?'scroll':'visible'});
  return {...f,side,html};
}
{
  const f=noParentScrollFixture(),r=await bridgeRead(f);
  assert.equal(f.state.sideScrolls,1);assert.equal(f.state.documentScrolls,0);
  assert.equal(r.deepCoverage.rootScrollProgress.pendingIntent.selectionBasis,'active_note_scroller');
  assert.equal(r.deepCoverage.rootScrollProgress.pendingIntent.selectedBefore.clientHeight,400);
  assert.ok(r.deepCoverage.rootScrollProgress.pendingIntent.candidates.length<=12);
}
{
  const f=noParentScrollFixture({commentsAncestor:true}),r=await bridgeRead(f);
  assert.equal(f.state.sideScrolls,1);assert.equal(f.state.documentScrolls,0);
  assert.equal(r.deepCoverage.rootScrollProgress.pendingIntent.selectionBasis,'comments_ancestor');
}
for(const options of [{outsideScope:true},{zeroHeight:true}]) {
  const f=noParentScrollFixture(options),r=await bridgeRead(f);
  assert.equal(f.state.sideScrolls,0);assert.equal(f.state.documentScrolls,0);
  assert.equal(r.deepCoverage.checkpoint.reason,'comment_scroll_container_unconfirmed');
  assert.equal(r.deepCoverage.rootScrollProgress.lastSelection.outcome,'no_verified_scroll_container');
}
{
  const f=noParentScrollFixture(),stalled={observedRootIds:[],rootScrollProgress:{format:'rendered-root-scroll-v1',
    noteId:'a'.repeat(24),state:'stalled',attempts:3,stalledRounds:3,pendingIntent:null}};
  const normal=await bridgeRead(f,stalled);
  assert.equal(f.state.sideScrolls,0,'Normal work cannot clear a persistent root stall');
  assert.equal(normal.deepCoverage.rootScrollProgress.lastSelection.outcome,'diagnostic_only_stalled_no_action');
  const marker={kind:'single_bounded_root_repair_v1',probeId:'b'.repeat(64),noteId:'a'.repeat(24),
    acquisition:1,maxScrolls:1,allowScroll:true};
  const first=await bridgeRead(f,{...stalled,rootRepairProbe:marker});
  assert.equal(f.state.sideScrolls,1);assert.equal(first.deepCoverage.rootRepairObservation.scrollsSubmitted,1);
  assert.equal(first.deepCoverage.rootRepairObservation.priorStalledRounds,3);
  f.state.now+=1000;
  const second=await bridgeRead(f,{...savedProgress(first),rootRepairProbe:{...marker,acquisition:2,allowScroll:false}});
  assert.equal(f.state.sideScrolls,1,'The second acquisition is observation-only');
  assert.equal(second.deepCoverage.rootRepairObservation.scrollsSubmitted,0);
  assert.equal(second.deepCoverage.rootPaginationState,'stalled');
  assert.equal(second.deepCoverage.explicitEnd,false);
}
{
  const f=rootPagingFixture(),marker={kind:'single_bounded_root_repair_v1',probeId:'b'.repeat(64),
    noteId:'a'.repeat(24),acquisition:1,maxScrolls:1,allowScroll:true};
  const stalled={observedRootIds:[f.ids[0]],rootScrollProgress:{noteId:'a'.repeat(24),
    state:'stalled',attempts:3,stalledRounds:3,pendingIntent:null},rootRepairProbe:marker};
  const first=await bridgeRead(f,stalled);f.state.visible=2;f.state.now+=1000;
  const second=await bridgeRead(f,{...savedProgress(first),rootRepairProbe:{...marker,acquisition:2,allowScroll:false}});
  assert.equal(f.state.scrolls,1);assert.equal(f.clickedIds.length,0);
  assert.ok(second.results.some(row=>row.commentId===f.ids[1]));
  assert.notEqual(second.deepCoverage.rootPaginationState,'stalled');
  assert.equal(second.deepCoverage.rootRepairObservation.scrollsSubmitted,0);
}
{
  const f=emptyCommentFixture({emptyText:'暂无评论'}),r=await bridgeRead(f,{rootScrollProgress:{
    noteId:'a'.repeat(24),state:'stalled',attempts:3,stalledRounds:3,pendingIntent:null},rootRepairProbe:{
    kind:'single_bounded_root_repair_v1',probeId:'b'.repeat(64),noteId:'a'.repeat(24),acquisition:2,maxScrolls:1,allowScroll:false}});
  assert.equal(r.deepCoverage.rootPaginationState,'complete');
  assert.equal(r.deepCoverage.rootEndBasis,'rendered_comment_empty_state');
  assert.equal(r.deepCoverage.rootRepairObservation.scrollsSubmitted,0);
}
{
  const f=fixture(false,{advancingClock:true,neverCallback:true,deferReplies:true,realIds:true});
  f.context.performance={timeOrigin:5000};const first=await bridgeRead(f),progress=savedProgress(first);
  f.state.now+=86400000;
  const same=await bridgeRead(f,progress);
  assert.equal(f.state.clicks,1,'Age alone within the same document cannot replay a submitted click');
  f.context.performance.timeOrigin=f.state.now-1000;
  const replaced=await bridgeRead(f,savedProgress(same));
  assert.equal(f.state.clicks,2,'Only the strictly later actual document permits a fresh control action');
  const t=replaced.deepCoverage.threads[0];
  assert.equal(t.retiredReplyExpansions[0].state,'retired_by_replacement_document');
  assert.equal(t.retiredReplyExpansions[0].clickedAt,10000);
  assert.equal(t.pendingReplyExpansion.documentTimeOrigin,f.context.performance.timeOrigin);
  f.state.now+=10000;await bridgeRead(f,savedProgress(replaced));
  assert.equal(f.state.clicks,2,'The replacement document intent cannot itself replay in that document');
}
{
  const f=fixture(false,{advancingClock:true,neverCallback:true,deferReplies:true,realIds:true});
  const first=await bridgeRead(f);f.state.now+=86400000;
  const noEpoch=await bridgeRead(f,savedProgress(first));
  assert.equal(f.state.clicks,1,'Missing actual timeOrigin evidence cannot retire an old intent');
  assert.equal(noEpoch.deepCoverage.documentObservation.timeOrigin,null);
}
{
  const f=fixture(false,{advancingClock:true,neverCallback:true,deferReplies:true,realIds:true});
  f.context.performance={timeOrigin:5000};const first=await bridgeRead(f);
  f.state.now+=10000;f.context.performance.timeOrigin=f.state.now-1000;f.state.replies=3;
  const late=await bridgeRead(f,savedProgress(first));
  assert.equal(f.state.clicks,1,'Late real reply IDs resolve the old intent without replaying it');
  assert.equal(late.deepCoverage.threads[0].retiredReplyExpansions,undefined);
  assert.equal(late.deepCoverage.threads[0].lastReplyExpansion.progressBasis,'new_rendered_reply_id');
}
{
  const f = rootPagingFixture();
  let result = await bridgeRead(f), progress = savedProgress(result);
  assert.equal(f.state.scrolls, 1);
  let cursor = result.deepCoverage.rootScrollProgress;
  assert.equal(cursor.pendingIntent.state, 'scroll_submitted');
  assert.equal(cursor.attempts, 1);assert.equal(cursor.stalledRounds, 0);
  assert.equal(cursor.pendingIntent.selectionBasis, 'comments_ancestor');
  assert.equal(cursor.pendingIntent.selectedBefore.scrollTop, 0);
  assert.equal(cursor.pendingIntent.selectedAfter.scrollTop, 600);
  assert.equal(cursor.pendingIntent.selectedBefore.rect.height, 400);
  assert.equal(cursor.pendingIntent.selectedBefore.visibleWidth, 300);
  assert.equal(cursor.pendingIntent.selectedBefore.visibleHeight, 400);
  assert.equal(cursor.pendingIntent.candidates[0].eligible, true);
  // A premature caller does not submit the same scroll again or count a stall.
  f.state.now += 100;
  result = await bridgeRead(f, progress);progress = savedProgress(result);
  assert.equal(f.state.scrolls, 1);assert.equal(result.deepCoverage.rootScrollProgress.stalledRounds, 0);
  assert.equal(result.deepCoverage.checkpoint.requestedWaitMs, 300);
  for (let completed = 1; completed <= 3; completed++) {
    f.state.now += 10000;
    result = await bridgeRead(f, progress);progress = savedProgress(result);
    assert.equal(result.deepCoverage.stalledScrollRounds, completed, 'Each prior scroll is settled only once');
    assert.equal(result.deepCoverage.explicitEnd, false);
    assert.notEqual(result.deepCoverage.rootPaginationState, 'complete', 'Repeated roots never prove platform end');
  }
  cursor = result.deepCoverage.rootScrollProgress;
  assert.equal(cursor.state, 'stalled');assert.equal(cursor.attempts, 3);
  assert.equal(cursor.pendingIntent, null);assert.equal(cursor.stalledRounds, 3);
  assert.equal(cursor.lastObservation.state, 'no_new_observed_root_ids');
  assert.equal(result.deepCoverage.rootPaginationState, 'stalled');
  assert.equal(result.deepCoverage.checkpoint.reason, 'root_pagination_stalled');
  assert.equal(f.state.scrolls, 3);
  f.state.now += 10000;
  result = await bridgeRead(f, progress);
  assert.equal(f.state.scrolls, 3, 'A persisted root stall cannot restart the scroll cycle');
  assert.equal(result.deepCoverage.rootScrollProgress.stalledRounds, 3);
  assert.equal(result.results.length, 1, 'Collected content remains available on a diagnostic handoff');
  // A subsequently observed real end is accepted; no inferred end was created.
  f.state.end = true;
  result = await bridgeRead(f, savedProgress(result));
  assert.equal(result.deepCoverage.explicitEnd, true);
  assert.equal(result.deepCoverage.rootPaginationState, 'complete');
  assert.equal(result.deepCoverage.rootScrollProgress.stalledRounds, 0);
  assert.equal(f.state.scrolls, 3);
}
{
  const f = rootPagingFixture();
  let result = await bridgeRead(f), progress = savedProgress(result);
  f.state.now += 10000;
  result = await bridgeRead(f, progress);progress = savedProgress(result);
  assert.equal(result.deepCoverage.rootScrollProgress.stalledRounds, 1);
  f.state.visible = 2;f.state.now += 10000;
  result = await bridgeRead(f, progress);
  assert.equal(result.deepCoverage.rootScrollProgress.stalledRounds, 0, 'A genuinely new rendered root clears its stall');
  assert.equal(result.deepCoverage.rootScrollProgress.lastObservation.state, 'new_root_ids_observed');
  assert.deepEqual(Array.from(result.deepCoverage.rootScrollProgress.lastObservation.newRootIds), [f.ids[1]]);
  assert.equal(result.deepCoverage.rootPaginationState, 'pending');
  assert.equal(result.deepCoverage.explicitEnd, false);
  assert.equal(result.results.length, 2);
}

// Web-only collection can finish the visible scope without inventing API reply totals.
for (const ended of [false,true]) {
  const f=manyRootsFixture(1);f.parents[0].querySelectorAll=()=>[];
  const old=f.context.document.querySelectorAll;
  f.context.document.querySelectorAll=selector=>selector.includes('.comments-container .end-container') && ended ? [new f.Element('- THE END -')] : old(selector);
  const r=await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs,{roots:1,maxReplyClicks:5,budgetMs:10000}),f.context);
  assert.equal(r.deepCoverage.explicitEnd,ended);
  assert.equal(r.deepCoverage.threads[0].expectedReplies,null);
  assert.equal(r.deepCoverage.threads[0].state,ended?'visible_replies_read':'visible_scope_pending_end');
  if(ended)assert.equal(r.deepCoverage.threads[0].completionBasis,'rendered_scope_end_no_expander_reply_total_unknown');
}
// Without API totals, already captured no-expander roots must not trap each batch at the first 30.
{
 const f=manyRootsFixture(45);for(const root of f.parents)root.querySelectorAll=()=>[];
 const first=await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs,{roots:30,maxReplyClicks:5,budgetMs:10000}),f.context);
 assert.equal(first.deepCoverage.rootsProcessed,30);
 const progress={threads:Object.fromEntries(first.deepCoverage.threads.map(t=>[t.rootKey,t])),observedRootIds:first.deepCoverage.observedRootIds};
 const second=await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs,{roots:30,maxReplyClicks:5,budgetMs:10000,progress}),f.context);
 assert.equal(second.deepCoverage.rootsProcessed,15);
 assert.equal(second.deepCoverage.threads[44].state,'visible_scope_pending_end');
 assert.notEqual(second.deepCoverage.rootPaginationState,'complete');
}
// Pure rendered emoji images have accessible labels but no textContent. Read
// only actual labels as text; missing observed sources remain body/media gaps.
async function emojiRowFixture({text = '', labels = [{alt: '[笑哭R]'}], ordinaryOutside = false, hidden = false, rejectAltRead = false} = {}) {
  const fixture = manyRootsFixture(1);
  const parent = fixture.parents[0], item = parent.querySelector('.comment-item');
  const originalQuery = item.querySelector;
  const content = new fixture.Element(text);
  let labelReads = 0;
  const imgs = labels.map(attributes => {
    const img = new fixture.Element('', attributes);
    img.classList = {contains: () => false};
    img.closest = selector => selector === '.content, .note-text' ? (ordinaryOutside ? null : content) : ordinaryOutside ? parent : null;
    img.checkVisibility = () => !hidden;
    img.getClientRects = () => hidden ? [] : [{}];
    img.getAttribute = name => {
      if (['alt', 'aria-label', 'title'].includes(name)) {
        labelReads++;
        if (rejectAltRead) throw new Error('MIXED_TEXT_MUST_NOT_READ_IMAGE_LABEL');
      }
      return attributes[name] || '';
    };
    if (ordinaryOutside) img.currentSrc = 'https://example.test/actual-comment-image.jpg';
    else Object.defineProperty(img, 'src', {get() {throw new Error('EMOJI_SRC_MUST_NOT_BE_READ_OR_GUESSED');}});
    return img;
  });
  content.querySelectorAll = selector => selector === 'img' && !ordinaryOutside ? imgs : [];
  item.querySelectorAll = selector => selector === 'img' ? imgs : [];
  item.querySelector = selector => selector.includes('.content') || selector.includes('.note-text') ? content : originalQuery(selector);
  parent.querySelectorAll = () => [];
  const result = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
    roots: 1, maxReplyClicks: 0, budgetMs: 10000,
    progress: {expectedRootIds: fixture.ids, apiRootsComplete: true, expectedReplyCounts: {[fixture.ids[0]]: 0}},
  }), fixture.context);
  return {result, labelReads};
}
const pureEmoji = await emojiRowFixture();
assert.equal(pureEmoji.result.results[0].text, '[笑哭R]');
assert.equal(pureEmoji.result.results[0].textSource, 'rendered_emoji_alt');
assert.equal(pureEmoji.result.deepCoverage.threads[0].state, 'visible_replies_read');
const unicodeEmoji = await emojiRowFixture({labels: [{alt: '😂'}]});
assert.equal(unicodeEmoji.result.results[0].text, '😂');
const labelledEmoji = await emojiRowFixture({labels: [{'aria-label': '[笑哭R]'}]});
assert.equal(labelledEmoji.result.results[0].textSource, 'rendered_emoji_accessible_label');
assert.equal(labelledEmoji.result.results[0].renderedImageLabels[0].attribute, 'aria-label');
for (const labels of [[{alt: ''}], [{alt: '\u200D'}], [{alt: '图片'}], [{alt: '普通照片'}], [{alt: '[笑哭R]'}, {alt: ''}]]) {
  const missing = await emojiRowFixture({labels});
  assert.equal(missing.result.results.length, 0, 'Unlabelled/generic pictures must not fabricate comment text');
  assert.equal(missing.result.deepCoverage.threads[0].reason, 'root_content_not_rendered');
}
const hiddenEmoji = await emojiRowFixture({hidden: true});
assert.equal(hiddenEmoji.result.results.length, 0, 'Hidden image labels are not rendered comment text');
const ordinaryImage = await emojiRowFixture({ordinaryOutside: true, labels: [{alt: '[笑哭R]'}]});
assert.equal(ordinaryImage.result.results[0].text, '[图片评论]');
assert.equal(ordinaryImage.result.results[0].textSource, 'media_placeholder');
assert.equal(ordinaryImage.labelReads, 0, 'Ordinary media keeps its actual image path instead of fallback text');
const mixedEmoji = await emojiRowFixture({text: '原有可见文字', rejectAltRead: true});
assert.equal(mixedEmoji.result.results[0].text, '原有可见文字');
assert.equal(mixedEmoji.result.results[0].textSource, 'rendered_text');
assert.equal(mixedEmoji.labelReads, 0, 'Do not append/rewrite existing text with fallback labels');

// Reply-prefix DOM shape: SPAN "回复", direct SPAN.nickname, text colon,
// SPAN.note-text containing an unlabelled IMG.note-content-emoji.
async function replyBodyFixture({body = '', wrapper = true, labels = [], progress = {}, nestedNickname = false, ordinaryOutside = false, authorId = 'cccccccccccccccccccccccc', cid = '777777777777777777777777', rootId, target = 'Fixture recipient'} = {}) {
  const f = manyRootsFixture(1), parent = f.parents[0];
  if (rootId) {
    f.ids[0] = rootId;
    parent.querySelector('.comment-item').attributes['data-comment-id'] = rootId;
  }
  const content = new f.Element((wrapper ? '回复 ' + target + ' : ' : '') + body);
  const nick = new f.Element(target);
  nick.parentElement = nestedNickname ? new f.Element() : content;
  const noteText = new f.Element(body);
  content.childNodes = wrapper ? [new f.Element(''), new f.Element('回复 '), nick,
    {nodeType: 3, textContent: ' : '}, new f.Element(''), noteText] : [{nodeType: 3, textContent: body}];
  const imgs = labels.map(attributes => {
    const img = new f.Element('', attributes);
    img.classList = {contains: name => !ordinaryOutside && name === 'note-content-emoji'};
    img.closest = selector => selector === '.content, .note-text' ? (ordinaryOutside ? null : content) : ordinaryOutside ? parent : null;
    img.checkVisibility = () => !attributes.hidden;
    img.getClientRects = () => attributes.hidden ? [] : [{}];
    if (ordinaryOutside) img.currentSrc = 'https://example.test/actual-reply-image.jpg';
    else if (attributes.currentSrc) img.currentSrc = attributes.currentSrc;
    else Object.defineProperty(img, 'src', {get() {throw new Error('REPLY_EMOJI_SRC_MUST_NOT_BE_READ');}});
    return img;
  });
  noteText.childNodes = [{nodeType: 3, textContent: body}, ...imgs];
  content.querySelector = selector => selector === ':scope > .nickname' && wrapper && !nestedNickname ? nick : null;
  content.querySelectorAll = selector => selector === 'img' && !ordinaryOutside ? imgs : [];
  const item = new f.Element('', {'data-comment-id': cid});
  item.querySelector = selector => selector.includes('href*=') ? new f.Element('', {href: '/user/profile/' + authorId})
    : selector.includes('.content') || selector.includes('.note-text') ? content
    : selector.includes('.name') ? new f.Element('Reply author') : null;
  item.querySelectorAll = selector => selector === 'img' ? imgs : [];
  parent.querySelectorAll = selector => selector.includes('.comment-item-sub') ? [item] : [];
  const result = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
    roots: 1, maxReplyClicks: 0, budgetMs: 10000, progress: {
      expectedRootIds: f.ids, apiRootsComplete: true, expectedReplyCounts: {[f.ids[0]]: 1}, ...progress,
    },
  }), f.context);
  return {result, rootId: f.ids[0], cid};
}
const replyEmoji = await replyBodyFixture({labels: [{alt: '[笑哭R]'}]});
assert.equal(replyEmoji.result.results.find(row => row.is_reply).text, '[笑哭R]');
assert.equal(replyEmoji.result.results.find(row => row.is_reply).reply_to, 'Fixture recipient');
assert.equal(replyEmoji.result.deepCoverage.threads[0].state, 'visible_replies_read');
for (const labels of [[], [{alt: ''}], [{alt: '照片'}], [{alt: '[笑哭R]', hidden: true}], [{alt: '[笑哭R]'}, {alt: ''}]]) {
  const gap = await replyBodyFixture({labels});
  assert.equal(gap.result.results.filter(row => row.is_reply).length, 0, 'A nickname prefix is not a reply body');
  const t = gap.result.deepCoverage.threads[0];
  assert.equal(t.reason, 'reply_content_not_rendered');
  assert.equal(t.replyBodyGaps[0].commentId, gap.cid);
  assert.equal(t.replyBodyGaps[0].reply_to, 'Fixture recipient');
  assert.equal(t.replyBodyGaps[0].renderedReplyPrefixOnly, true);
}
const literalReply = await replyBodyFixture({wrapper: false, body: '回复 Fixture recipient :'});
assert.equal(literalReply.result.results.find(row => row.is_reply).text, '回复 Fixture recipient :');
assert.equal(literalReply.result.deepCoverage.threads[0].replyContentCaptured, true);
const realReply = await replyBodyFixture({body: '有真实正文', labels: [{alt: ''}]});
assert.equal(realReply.result.results.find(row => row.is_reply).text, '回复 Fixture recipient : 有真实正文');
const smallerReply = await replyBodyFixture({progress: {threads: Object.fromEntries(realReply.result.deepCoverage.threads.map(t => [t.rootKey, t]))}});
assert.equal(smallerReply.result.deepCoverage.threads[0].state, 'previously_read', 'A same-ID, same-UID body receipt survives a smaller view');
assert.equal(smallerReply.result.deepCoverage.threads[0].replyBodyGaps.length, 0);
assert.equal(smallerReply.result.deepCoverage.threads[0].currentReplyBodyGaps[0].retainedPriorBody, true);
const legacyPrefix = await replyBodyFixture({progress: {threads: {['id:' + realReply.rootId]: {
  rootKey: 'id:' + realReply.rootId, commentId: realReply.rootId, state: 'visible_replies_read', visibleReplies: 1, replyContentCaptured: true,
}}}});
assert.equal(legacyPrefix.result.deepCoverage.threads[0].state, 'stalled', 'Legacy booleans do not prove a body was captured');
assert.equal(legacyPrefix.result.deepCoverage.remainingThreads.length, 1);
const changedAuthor = await replyBodyFixture({authorId: 'dddddddddddddddddddddddd', progress: {
  threads: Object.fromEntries(realReply.result.deepCoverage.threads.map(t => [t.rootKey, t])),
}});
assert.equal(changedAuthor.result.deepCoverage.threads[0].reason, 'reply_content_not_rendered', 'Same ID with a different UID cannot inherit a body');
const mediaReply = await replyBodyFixture({ordinaryOutside: true, labels: [{}], progress: {
  threads: {['id:' + realReply.rootId]: {rootKey: 'id:' + realReply.rootId, commentId: realReply.rootId,
    state: 'stalled', replyBodyMinimumCount: 1, legacyReplyBodyIds: [realReply.cid]}},
}});
assert.equal(mediaReply.result.results.find(row => row.is_reply).textSource, 'media_placeholder');
assert.equal(mediaReply.result.deepCoverage.threads[0].state, 'visible_replies_read', 'Actual current media capture resolves a capture gap in this batch');
assert.equal(mediaReply.result.deepCoverage.threads[0].replyBodyCaptures[realReply.cid].images.length, 1);
// Synthetic DOM shapes: one reply-prefix emoji and three repeated emoji with
// no text. Shared fixtures contain no research account/comment identities.
const inlineCases = JSON.parse(fs.readFileSync(new URL('./fixtures/inline-emoji-dom.json', import.meta.url), 'utf8'));
for (const sample of inlineCases) {
  const captured = await replyBodyFixture(sample);
  const row = captured.result.results.find(r => r.is_reply);
  assert.equal(row.commentId, sample.cid);
  assert.equal(row.parentCommentId, sample.rootId);
  assert.equal(row.authorHrefRaw, '/user/profile/' + sample.authorId);
  assert.equal(row.text, '[图片评论]', 'Placeholder is explicitly non-text evidence');
  assert.equal(row.textSource, 'media_placeholder');
  assert.equal(row.renderedImageLabels, undefined, 'No invented label or emotion');
  assert.equal(row.renderedInlineImages.length, sample.labels.length, 'Keep repeated DOM occurrences');
  assert.deepEqual(Array.from(row.renderedInlineImages, image => image.url), sample.labels.map(image => image.src));
  assert.deepEqual(Array.from(row.renderedInlineImages, image => image.domOrdinal), sample.labels.map((_, i) => i + 1));
  assert.equal(captured.result.deepCoverage.threads[0].state, 'visible_replies_read', 'This state certifies collection only');
  const progress = {threads: Object.fromEntries(captured.result.deepCoverage.threads.map(t => [t.rootKey, t]))};
  const resumed = await replyBodyFixture({...sample, progress});
  assert.equal(resumed.result.deepCoverage.threads[0].state, 'previously_read', 'Same-parameter continuation must not stall again');
  assert.equal(resumed.result.deepCoverage.threads[0].replyBodyGaps.length, 0);
  const smaller = await replyBodyFixture({...sample, labels: [], progress});
  assert.equal(smaller.result.deepCoverage.threads[0].currentReplyBodyGaps[0].retainedPriorBody, true);
  assert.equal(smaller.result.deepCoverage.threads[0].replyBodyCaptures[sample.cid].renderedInlineImages.length, sample.labels.length);
  const changedUid = await replyBodyFixture({...sample, labels: [], authorId: 'd'.repeat(24), progress});
  assert.equal(changedUid.result.deepCoverage.threads[0].reason, 'reply_content_not_rendered', 'Do not inherit another UID evidence');
  const hidden = await replyBodyFixture({...sample, labels: sample.labels.map(image => ({...image, hidden: true}))});
  assert.equal(hidden.result.results.filter(r => r.is_reply).length, 0, 'A hidden source is not observed comment content');
}
const mixedInline = await replyBodyFixture({body: 'Visible words', labels: inlineCases[0].labels});
assert.equal(mixedInline.result.results.find(r => r.is_reply).textSource, 'rendered_text');
assert.equal(mixedInline.result.results.find(r => r.is_reply).renderedInlineImages.length, 1, 'Visible words do not silently erase inline media');
const accessibleInline = await replyBodyFixture({labels: [{...inlineCases[0].labels[0], alt: '[笑哭R]'}]});
assert.equal(accessibleInline.result.results.find(r => r.is_reply).text, '[笑哭R]');
assert.equal(accessibleInline.result.results.find(r => r.is_reply).renderedInlineImages, undefined, 'Fully captured accessible labels do not need redundant media tasks');
const mixedLabels = await replyBodyFixture({labels: [{...inlineCases[0].labels[0], alt: '[笑哭R]'}, inlineCases[0].labels[0]]});
assert.equal(mixedLabels.result.results.find(r => r.is_reply).renderedInlineImages.length, 2, 'Incomplete mixed labels retain the full visible sequence');
const missingInlineSource = await replyBodyFixture({labels: [inlineCases[0].labels[0], {}]});
assert.equal(missingInlineSource.result.results.find(r => r.is_reply).renderedInlineImages.length, 2, 'Unavailable adjacent sources remain explicit media gaps');
const mixedWithoutSource = await replyBodyFixture({body: 'Visible words', labels: [{}]});
assert.equal(mixedWithoutSource.result.results.find(r => r.is_reply).renderedInlineImages.length, 1, 'Visible text cannot erase an inline image with unavailable source');
assert.equal(mixedWithoutSource.result.results.find(r => r.is_reply).renderedInlineImages[0].url, '');
const absentLegacy = manyRootsFixture(1);
absentLegacy.parents[0].querySelectorAll = () => [];
const legacyKey = 'id:' + absentLegacy.ids[0];
const absentLegacyResult = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
  roots: 1, maxReplyClicks: 0, budgetMs: 10000, progress: {
    apiRootsComplete: true, expectedRootIds: absentLegacy.ids, expectedReplyCounts: {[absentLegacy.ids[0]]: 0},
    threads: {[legacyKey]: {rootKey: legacyKey, commentId: absentLegacy.ids[0], state: 'stalled',
      replyBodyMinimumCount: 1, legacyReplyBodyIds: ['777777777777777777777777']}},
  },
}), absentLegacy.context);
assert.equal(absentLegacyResult.deepCoverage.threads[0].reason, 'reply_content_not_rendered', 'A smaller DOM with no reply cannot erase a known old missing body');
assert.equal(absentLegacyResult.deepCoverage.threads[0].replyBodyGaps[0].reason, 'prior_reply_body_unverified');
const absentRoot = manyRootsFixture(1);
absentRoot.parents[0].querySelectorAll = () => [];
const absentScope = new absentRoot.Element();
const absentDocument = {tagName: 'HTML', className: '', scrollTop: 0, scrollHeight: 1600,
  clientHeight: 600, clientWidth: 800, contains: element => element === absentScope,
  getBoundingClientRect: () => ({top: 0, left: 0, width: 800, height: 1600, bottom: 1600, right: 800}),
  scrollTo(x, y) {this.scrollTop = Math.min(y, 1000);}};
absentRoot.context.document.scrollingElement = absentDocument;
const absentQuery = absentRoot.context.document.querySelector;
absentRoot.context.document.querySelector = selector => selector.includes('#noteContainer') ? absentScope : absentQuery(selector);
const unseenId = 'eeeeeeeeeeeeeeeeeeeeeeee';
const absentRootResult = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
  roots: 2, maxReplyClicks: 1, budgetMs: 10000, progress: {
    apiRootsComplete: true, expectedRootIds: [...absentRoot.ids, unseenId], observedRootIds: [unseenId],
    expectedReplyCounts: {[absentRoot.ids[0]]: 0, [unseenId]: 1},
    threads: {['id:' + unseenId]: {rootKey: 'id:' + unseenId, commentId: unseenId, state: 'stalled',
      replyBodyMinimumCount: 1, legacyReplyBodyIds: ['777777777777777777777777']}},
  },
}), absentRoot.context);
assert.ok(absentRootResult.deepCoverage.scrollRounds > 0, 'A reopened old floor absent from the DOM must be sought, not skipped by prior API roots-complete');

// Zero-click snapshots expose all loaded roots, including unselected later floors.
const bulk = manyRootsFixture();
const bulkFirst = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
  roots: 200, maxReplyClicks: 0, budgetMs: 150000,
}), bulk.context);
assert.equal(bulkFirst.results.filter(x => !x.is_reply).length, 245);
assert.equal(bulkFirst.deepCoverage.rootsProcessed, 200);
assert.ok(bulkFirst.results.some(x => x.text.includes('root 21')));
assert.equal(bulkFirst.deepCoverage.threads.filter(x => x.state === 'not_processed').length, 45);
assert.equal(bulkFirst.results[20].commentId, bulk.ids[20]);

// Keep genuine completed IDs, then reorder the DOM. Resume must skip identities,
// not old list offsets. One click per root caps this fixture by elapsed time.
const batchOne = manyRootsFixture(245);
const first = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
  roots: 20, maxReplyClicks: 30, budgetMs: 150000,
  progress: {expectedReplyCounts: Object.fromEntries(batchOne.ids.map(id => [id, 0]))},
}), batchOne.context);
const progress = {
  threads: Object.fromEntries(first.deepCoverage.threads.map(x => [x.rootKey, x])),
  observedRootIds: first.deepCoverage.observedRootIds,
  expectedRootIds: batchOne.ids, apiRootsComplete: true,
  expectedReplyCounts: Object.fromEntries(batchOne.ids.map(id => [id, 0])),
};
const reordered = manyRootsFixture(245, true);
const second = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
  roots: 20, maxReplyClicks: 30, budgetMs: 150000, progress,
}), reordered.context);
assert.equal(reordered.clickedIds.length, 20);
assert.ok(reordered.clickedIds.every(id => !batchOne.clickedIds.includes(id)), 'Reordering must not cause prior completed root clicks');
assert.ok(reordered.clickedIds.includes(reordered.ids[244]), 'Can process beyond root 200 in a subsequent bounded batch');
assert.equal(second.deepCoverage.rootPaginationState, 'complete', 'Complete here means known API root IDs accounted for');
assert.ok(second.deepCoverage.remainingThreads.length > 0, 'Known roots do not imply all replies read');

// Resuming an interrupted one-click thread with the same parameters continues it.
const interrupted = fixture();
const part = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {roots: 1, maxReplyClicks: 1, budgetMs: 10000}), interrupted.context);
const resumed = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
  roots: 1, maxReplyClicks: 1, budgetMs: 10000,
  progress: {threads: Object.fromEntries(part.deepCoverage.threads.map(x => [x.rootKey, x]))},
}), interrupted.context);
assert.equal(interrupted.state.clicks, 2);
assert.equal(resumed.deepCoverage.threads[0].state, 'pending');
assert.equal(resumed.results[0].commentId, '', 'A fallback fingerprint is not a real ID');
assert.equal(resumed.results[0].identityKind, 'uncertain_fingerprint');


const scopeChanged = fixture(false, {navigateOnExpand: true, rejectPrivateRead: true});
const scopeResult = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
  roots: 1, maxReplyClicks: 10, budgetMs: 10000,
  url: 'https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa',
}), scopeChanged.context);
assert.equal(scopeResult.scopeChanged, true);
assert.equal(scopeChanged.state.clicks, 1, 'No more clicks after the note scope changes');
assert.ok(scopeResult.partial.results.some(x => x.text === 'Which doctor?'), 'Preserve earlier public-note evidence');
const alreadyPrivate = fixture(false, {rejectPrivateRead: true});
alreadyPrivate.context.location.href = 'https://www.xiaohongshu.com/chat/aaaaaaaaaaaaaaaaaaaaaaaa';
const noPrivateRead = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
  roots: 1, maxReplyClicks: 10, budgetMs: 10000,
  url: 'https://www.xiaohongshu.com/explore/aaaaaaaaaaaaaaaaaaaaaaaa',
}), alreadyPrivate.context);
assert.equal(noPrivateRead.scopeChanged, true);
assert.equal(noPrivateRead.partial.results.length, 0);
const linkedExpander = fixture(false, {linkExpander: true});
const linkedResult = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
  roots: 1, maxReplyClicks: 10, budgetMs: 10000,
}), linkedExpander.context);
assert.equal(linkedExpander.state.clicks, 0, 'An expander-shaped link must never be clicked');
assert.equal(linkedResult.deepCoverage.threads[0].state, 'stalled');
assert.equal(linkedResult.deepCoverage.threads[0].reason, 'unsafe_expander_not_clicked');
assert.equal(classifyReadError('NOTE_SCOPE_CHANGED'), 'NOTE_SCOPE_CHANGED');

const transientStall = fixture();
const stalledKey = resumed.deepCoverage.threads[0].rootKey;
const stalledProgress = {threads: {[stalledKey]: {state: 'stalled'}}};
const retriedStall = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
  roots: 1, maxReplyClicks: 1, budgetMs: 10000, progress: stalledProgress,
}), transientStall.context);
assert.equal(transientStall.state.clicks, 1, 'Explicit resume can retry a previously stalled thread');
const automaticStall = fixture();
await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
  roots: 1, maxReplyClicks: 1, budgetMs: 10000, progress: {...stalledProgress, retryStalled: false},
}), automaticStall.context);
assert.equal(automaticStall.state.clicks, 0, 'Automatic batches leave stalled threads for explicit review');

const growing = manyRootsFixture(1);
const growingId = growing.ids[0];
await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
  roots: 1, maxReplyClicks: 1, budgetMs: 10000,
  progress: {threads: {['id:' + growingId]: {state: 'visible_replies_read', visibleReplies: 2}},
    expectedReplyCounts: {[growingId]: 10}},
}), growing.context);
assert.equal(growing.clickedIds.length, 1, 'Fresh API reply count growth reopens a previously read root');

// The previous batch saw 35 root IDs, but a reloaded page starts with only 10.
// Root 21 is still unfinished: history must not terminate this pass at screen 1.
for (const uiTotal of [35, 0]) {
const pagedResume = manyRootsFixture(35);
let visibleRoots = 10, resumeScrolls = 0;
const resumeScroller = {tagName:'DIV',className:'note-scroller',scrollHeight:5000,clientHeight:400,clientWidth:300,
  getBoundingClientRect:()=>({top:20,left:10,width:300,height:400,bottom:420,right:310}),scrollTo() {
  resumeScrolls++; visibleRoots = Math.min(35, visibleRoots + 10);
}};
const resumeScope=new pagedResume.Element();resumeScope.contains=element=>element===resumeScroller;
const originalQuery = pagedResume.context.document.querySelector;
pagedResume.context.document.querySelector = selector => selector === '.note-scroller' ? resumeScroller
  : selector.includes('#noteContainer') ? resumeScope
  : selector.includes('.total') ? new pagedResume.Element('共' + uiTotal + '条评论') : originalQuery(selector);
pagedResume.context.window.getComputedStyle=element=>({overflowY:element===resumeScroller?'scroll':'visible'});
pagedResume.context.document.querySelectorAll = selector => selector === '.parent-comment' ? pagedResume.parents.slice(0, visibleRoots) : [];
const resumeThreads = Object.fromEntries(pagedResume.ids.map((id, i) => ['id:' + id, {
  commentId: id, state: i === 20 ? 'stalled' : 'visible_replies_read', visibleReplies: 0,
}]));
const reached = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
  roots: 5, maxReplyClicks: 5, budgetMs: 150000,
  progress: {threads: resumeThreads, observedRootIds: pagedResume.ids, expectedRootIds: pagedResume.ids,
    apiRootsComplete: true, retryStalled: true, expectedReplyCounts: Object.fromEntries(pagedResume.ids.map(id => [id, 0]))},
}), pagedResume.context);
assert.equal(resumeScrolls, 2, 'Resume must reach the known unfinished root below the first screen');
assert.deepEqual(pagedResume.clickedIds, [pagedResume.ids[20]], 'Completed roots must not be expanded again');
assert.equal(reached.deepCoverage.threads.find(t => t.commentId === pagedResume.ids[20]).state, 'visible_replies_read');
}

// A reply can arrive during a later thread's await, after its own thread stalled.
// The captured row IDs, text, and safe DOM state must justify reclassification.
async function lateRenderedReplies(options = {}) {
  const f = manyRootsFixture(2);
  const id = f.ids[0], parent = f.parents[0], item = parent.querySelector('.comment-item');
  if (options.noRootId) item.getAttribute = () => '';
  const makeReply = i => {
    const cid = options.noReplyIds ? '' : (100 + (options.duplicateIds ? 0 : i)).toString(16).padStart(24, '0');
    const row = new f.Element('', {'data-comment-id': cid});
    row.querySelector = selector => selector.includes('.name') ? new f.Element('Reply ' + i)
      : selector.includes('.content') ? new f.Element(options.emptyReply ? '' : 'Late reply ' + i)
      : selector.includes('.date') ? new f.Element('2026-10-30') : null;
    return row;
  };
  let arrived = false;
  const expander = new f.Element('展开更多回复');
  expander.getAttribute = attr => options.unsafeExpander && attr === 'href' ? '/chat/private' : '';
  expander.click = () => {throw new Error('Deferred stalled root must not be clicked');};
  parent.querySelectorAll = selector => selector.includes('.comment-item-sub') ? Array.from({length: arrived ? 2 : 1}, (_, i) => makeReply(i))
    : selector.startsWith('button') && (options.safeExpander || options.unsafeExpander) ? [expander] : [];
  let rootKey = 'id:' + id;
  if (options.noRootId) {
    const initial = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {roots: 1, maxReplyClicks: 0, budgetMs: 1000}), f.context);
    rootKey = initial.deepCoverage.threads[0].rootKey;
  }
  const originalTimeout = f.context.setTimeout;
  f.context.setTimeout = (cb, ms) => {
    if (f.clickedIds.includes(f.ids[1])) arrived = true;
    originalTimeout(cb, ms);
  };
  const output = await vm.runInNewContext(buildDeepExtractor(buildCommentsExtractJs, {
    roots: 2, maxReplyClicks: 2, budgetMs: 10000,
    progress: {retryStalled: false, threads: options.firstBatch ? {} : {[rootKey]: {commentId: options.noRootId ? '' : id, state: 'stalled', visibleReplies: 1}},
      apiRootsComplete: true, expectedRootIds: f.ids, observedRootIds: f.ids,
      expectedReplyCounts: options.unknownExpected ? {} : {[id]: options.expected ?? 2}},
  }), f.context);
  return output.deepCoverage.threads.find(t => t.rootKey === rootKey);
}
const lateComplete = await lateRenderedReplies();
assert.equal(lateComplete.state, 'visible_replies_read');
assert.equal(lateComplete.uniqueObservedReplyCount, 2);
assert.equal(lateComplete.resolvedBy, 'captured_reply_ids_after_stall');
const newlyStalled = await lateRenderedReplies({firstBatch: true});
assert.equal(newlyStalled.processed, true, 'The thread was first attempted with only one reply');
assert.equal(newlyStalled.state, 'visible_replies_read', 'A later awaited thread may deliver the missing reply in the same batch');
assert.equal(newlyStalled.resolvedBy, 'captured_reply_ids_after_stall');
for (const options of [{unknownExpected: true}, {noRootId: true}, {noReplyIds: true}, {duplicateIds: true},
  {emptyReply: true}, {safeExpander: true}, {unsafeExpander: true}, {expected: 3}]) {
  const unresolved = await lateRenderedReplies(options);
  assert.equal(unresolved.state, 'stalled', 'Unproven late completeness must retain its gap: ' + JSON.stringify(options));
}
for (const options of [{unknownExpected: true}, {noRootId: true}, {noReplyIds: true}, {duplicateIds: true},
  {emptyReply: true}, {expected: 3}]) {
  const firstAttempt = await lateRenderedReplies({...options, firstBatch: true});
  assert.equal(firstAttempt.state, options.unknownExpected ? 'visible_scope_pending_end' : 'stalled', 'First expansion uses the same capture/ID/count standard: ' + JSON.stringify(options));
}
process.stdout.write('Deep-comment behavioral checks passed: rendered emoji alt/accessible labels, missing/hidden/ordinary/mixed-image boundaries; root 21 with normal/zero UI total, 10 late-render and 6 first-attempt outcomes, 7 login-wall variants; no network or browser used.\n');




const pythonChecks = String.raw`
import contextlib, json, sys, tempfile, subprocess
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import xhs_research
if not hasattr(xhs_research, 'account_lock'):
    xhs_research.account_lock = lambda run: contextlib.nullcontext()
import xhs_browser as b
base = {'expectedRootIds':['a'*24, 'b'*24], 'apiRootsComplete': True}
full = {'coverage': {'rootPaginationState':'complete', 'observedRootIds':['a'*24, 'b'*24],
    'threads': [{'rootKey':'id:'+'a'*24,'commentId':'a'*24,'state':'visible_replies_read','visibleReplies':5},
                {'rootKey':'id:'+'b'*24,'commentId':'b'*24,'state':'visible_replies_read','visibleReplies':2}]},
    'data':[{'commentId':'a'*24}, {'commentId':'c'*24}]}
first = b.merge_browser_progress(base, full, 'batch-1')
small = {'coverage': {'rootPaginationState':'pending','observedRootIds':['a'*24],
    'threads':[{'rootKey':'id:'+'a'*24,'commentId':'a'*24,'state':'not_processed','visibleReplies':0}]},
    'data':[{'commentId':'a'*24}]}
merged = b.merge_browser_progress(first, small, 'batch-2')
assert merged['status'] == 'visible_scope_complete'
assert len(merged['threads']) == 2
assert merged['threads']['id:'+'a'*24]['visibleReplies'] == 5
assert len(merged['observedCommentIds']) == 2
root_pending = {'format':'rendered-root-scroll-v1','noteId':'d'*24,'attempts':2,'stalledRounds':1,
    'state':'waiting_for_render','pendingIntent':{'attempt':2,'submittedAt':123,'beforeObservedRootIds':['a'*24]}}
root_first = b.merge_browser_progress(first, {'coverage':{'rootPaginationState':'pending',
    'rootScrollProgress':root_pending,'threads':[]},'data':[]}, 'root-batch-1')
assert root_first['rootScrollProgress'] == root_pending
root_smaller = b.merge_browser_progress(root_first, small, 'root-smaller-legacy')
assert root_smaller['rootScrollProgress'] == root_pending, 'A smaller/legacy snapshot cannot erase a root scroll intent'
assert len(root_smaller['threads']) == 2 and len(root_smaller['observedCommentIds']) == 2
root_stall = dict(root_pending,attempts=3,stalledRounds=3,state='stalled',pendingIntent=None)
root_after = b.merge_browser_progress({}, {'coverage':{'rootPaginationState':'stalled',
    'rootScrollProgress':root_stall,'threads':[]},'data':[]}, 'root-stalled')
assert root_after['status'] == 'stalled' and root_after['rootScrollProgress'] == root_stall
assert root_after['rootPaginationState'] != 'complete'
root_old_cache = b.merge_browser_progress(root_after, {'coverage':{'rootPaginationState':'pending',
    'rootScrollProgress':root_pending,'threads':[]},'data':[]}, 'older-root-cache')
assert root_old_cache['rootScrollProgress'] == root_stall and root_old_cache['status'] == 'stalled'
root_pure_reset = b.merge_browser_progress(root_after, {'coverage':{'rootPaginationState':'pending',
    'rootScrollProgress':dict(root_stall,stalledRounds=0,state='ready'),'threads':[]},'data':[]}, 'pure-counter-reset')
assert root_pure_reset['rootScrollProgress'] == root_stall and root_pure_reset['status'] == 'stalled'
root_real_end = b.merge_browser_progress(root_after, {'coverage':{'rootPaginationState':'complete','explicitEnd':True,
    'rootScrollProgress':dict(root_stall,stalledRounds=0,state='end_observed'),'threads':[]},'data':[]}, 'real-root-end')
assert root_real_end['rootScrollProgress']['stalledRounds'] == 0 and root_real_end['rootPaginationState'] == 'complete'
unknown = b.merge_browser_progress({}, {'coverage': {'rootPaginationState':'stalled','threads':[]},'data':[]}, 'unknown')
assert unknown['status'] == 'stalled'
with tempfile.TemporaryDirectory() as temp:
    run = Path(temp)
    b.write_json(run/'run.json', {'format':xhs_research.FORMAT,'directory':str(run),'closed':False})
    b.write_json(run/'browser.json', {'profile':str(run/'browser'),'pid':999,'node':'unused',
        'endpoint':'http://127.0.0.1:9222','opencli_root':'unused'})
    payload = {'ok':True,'coverage':full['coverage'],'data':[{'commentId':'a'*24,'is_reply':False}]}
    completed = subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr='')
    with patch.object(b, 'account_lock', lambda run: contextlib.nullcontext()), \
         patch.object(b, 'process_command', return_value='chrome --user-data-dir='+str(run/'browser')), \
         patch.object(b, 'bound_target', return_value={'target_id':'test_target','target_websocket':'ws://127.0.0.1:9222/devtools/page/test_target'}), \
         patch.object(b, 'resolve_note', return_value=('d'*24, {'url':'https://www.xiaohongshu.com/explore/'+'d'*24})), \
         patch.object(b.subprocess, 'run', return_value=completed) as mocked:
        one = b.deep_comments(run,'d'*24,20,30,90,resume=True)
        two = b.deep_comments(run,'d'*24,20,30,90,resume=True)
        assert one['file'] != two['file']
        assert mocked.call_count == 2, 'same resume parameters must execute another bounded batch'
        sent = json.loads(mocked.call_args[0][0][-1])
        assert sent['progress']['threads'], 'second batch receives persisted thread progress'
        before = Path(two['progress_file']).read_bytes()
        scope_payload = {'ok':False,'error':{'code':'NOTE_SCOPE_CHANGED','message':'NOTE_SCOPE_CHANGED'},
                         'partial':{'data':payload['data'],'coverage':{'rootPaginationState':'pending','scopeChanged':True}}}
        mocked.return_value = subprocess.CompletedProcess([],1,stdout=json.dumps(scope_payload),stderr='')
        try:
            b.deep_comments(run,'d'*24,20,30,90,resume=True)
        except b.NoteScopeChanged as exc:
            assert exc.code == 'NOTE_SCOPE_CHANGED'
        else:
            raise AssertionError('scope change must be a distinct failed batch')
        assert not (run/'STOP.json').exists(), 'Known navigation is not a platform restriction'
        assert Path(two['progress_file']).read_bytes() == before
        failed = [json.loads(p.read_text()) for p in (run/'raw').glob('*.json')]
        assert any(p.get('partial_prior_note_only') and p.get('data') for p in failed)
        mocked.side_effect = subprocess.TimeoutExpired('test', 1)
        try:
            b.deep_comments(run,'d'*24,20,30,90,resume=True)
        except b.ResearchError:
            pass
        else:
            raise AssertionError('timeout must not become an empty successful read')
        assert Path(two['progress_file']).read_bytes() == before
print('Browser Python persistence checks passed; transport was mocked.')
`;
const py = spawnSync(process.env.XHS_TEST_PYTHON || 'python3', ['-c', pythonChecks, path.dirname(fileURLToPath(import.meta.url))], {encoding: 'utf8'});
assert.equal(py.status, 0, py.stderr || py.stdout);
process.stdout.write(py.stdout);
