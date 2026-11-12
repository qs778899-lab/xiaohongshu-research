// Bounded rendered-UI expansion. Reuses the pinned adapter's extraction schema.
import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';

// This function is serialized into the owned page; only rendered DOM is read.
async function readRenderedBatch(options) {
  const startedAt = Date.now();
  const origin = typeof performance === 'object' ? performance.timeOrigin : null;
  const documentTimeOrigin = Number.isFinite(origin) && origin > 0 && origin <= startedAt + 1 ? origin : null;
  const withinBudget = () => Date.now() - startedAt < options.budgetMs;
  let checkpoint = null;
  const handoff = (reason, thread, waitMs = 0) => {
    checkpoint = {reason, requestedWaitMs: Math.max(0, waitMs), at: Date.now()};
    if (thread) {
      thread.processed = false;
      thread.state = 'pending';
      thread.reason = reason;
      checkpoint.rootKey = thread.rootKey;
    }
    throw Object.assign(new Error('DEEP_READ_CHECKPOINT'), {code: 'DEEP_READ_CHECKPOINT'});
  };
  const wait = async (ms, reason = 'render_wait_pending', thread) => {
    const remaining = options.budgetMs - (Date.now() - startedAt);
    if (remaining <= 0) handoff('time_budget_pending', thread);
    // A background page timer can be throttled past Runtime.evaluate's own
    // deadline. The bridge hands waiting back to its paced outer scheduler;
    // it never leaves an in-page timer or async continuation running behind it.
    if (options.yieldOnWait) handoff(reason, thread, Math.min(ms, remaining));
    await new Promise(resolve => setTimeout(resolve, Math.min(ms, remaining)));
    if (!withinBudget()) handoff('time_budget_pending', thread);
  };
  const prior = options.progress || {};
  const expectedNoteId = (options.noteUrl || location.href).match(/^https:\/\/www\.xiaohongshu\.com\/(?:explore|search_result|user\/profile\/[a-f0-9]{24})\/([a-zA-Z0-9]{16,40})(?:[/?#]|$)/)?.[1];
  const replyScope = prior.discoveredReplyScope;
  if (replyScope && (!options.yieldOnWait || replyScope.kind !== 'discovered_replies_v1' ||
      replyScope.noteId !== expectedNoteId || !/^[a-f0-9]{64}$/.test(replyScope.scopeId || '') ||
      !/^native-[a-f0-9]{20}$/.test(replyScope.taskId || '') || !['probe', 'continuation'].includes(replyScope.phase) ||
      replyScope.maxRootScrolls !== 0 || !Array.isArray(replyScope.rootIds) || !replyScope.rootIds.length ||
      !replyScope.rootIds.every(id => typeof id === 'string' && /^[a-fA-F0-9]{16,40}$/.test(id)) || prior.rootRepairProbe))
    throw new Error('DISCOVERED_REPLY_SCOPE_INVALID');
  const replyRootIds = new Set(replyScope?.rootIds || []), outsideReplyScope = new Set();
  let replyScopeState = 'visible_controls_pending';
  const repair = prior.rootRepairProbe;
  const rootRepairProbe = options.yieldOnWait && repair?.kind === 'single_bounded_root_repair_v1' &&
    /^[a-f0-9]{64}$/.test(repair.probeId || '') && repair.noteId === expectedNoteId &&
    [1, 2].includes(repair.acquisition) && repair.maxScrolls === 1 &&
    repair.allowScroll === (repair.acquisition === 1) ? repair : null;
  let repairScrolls = 0;
  const probeBaselineRootIds = new Set(prior.observedRootIds || []);
  const priorRootScroll = prior.rootScrollProgress || {};
  if (options.yieldOnWait && priorRootScroll.noteId && priorRootScroll.noteId !== expectedNoteId)
    throw new Error('ROOT_PROGRESS_SCOPE_MISMATCH');
  const rootScrollProgress = options.yieldOnWait ? {
    ...priorRootScroll, format: 'rendered-root-scroll-v1', noteId: expectedNoteId,
    attempts: Number.isInteger(priorRootScroll.attempts) && priorRootScroll.attempts >= 0 ? priorRootScroll.attempts : 0,
    stalledRounds: Number.isInteger(priorRootScroll.stalledRounds) && priorRootScroll.stalledRounds >= 0 ? priorRootScroll.stalledRounds : 0,
    state: priorRootScroll.state || 'ready',
    pendingIntent: priorRootScroll.pendingIntent ? {...priorRootScroll.pendingIntent,
      beforeObservedRootIds: [...(priorRootScroll.pendingIntent.beforeObservedRootIds || [])]} : null,
  } : null;
  const previousThreads = prior.threads || {};
  const replyVisits = new Map(Object.entries(previousThreads).filter(([key, thread]) => replyScope &&
    key === 'id:' + thread.commentId && replyRootIds.has(thread.commentId) &&
    thread.discoveredReplyVisit?.scopeId === replyScope.scopeId &&
    thread.discoveredReplyVisit?.rootCommentId === thread.commentId &&
    thread.discoveredReplyVisit?.state === 'captured_visible_scope')
    .map(([key, thread]) => [key, {...thread.discoveredReplyVisit}]));
  const previousObserved = new Set(prior.observedRootIds || []);
  const expectedIds = new Set(prior.expectedRootIds || []);
  // Discovery can be complete in history while this page has only reloaded its
  // first screen. A resumable unfinished root must be visited in this batch.
  const needsVisit = new Set();
  for (const [key, old] of Object.entries(previousThreads)) {
    const expected = prior.expectedReplyCounts?.[old.commentId || key.replace(/^id:/, '')];
    if (!['visible_replies_read', 'previously_read'].includes(old.state) ||
        (Number.isInteger(expected) && expected > (old.visibleReplies || 0))) needsVisit.add(key);
  }
  for (const id of expectedIds) if (!previousThreads['id:' + id]) needsVisit.add('id:' + id);
  const threads = new Map();
  const rows = new Map();
  const seenInPass = new Set();
  const pendingObservedThisBatch = new Set();
  const discoveryDeferredReplyRoots = new Map();
  const pendingObservationLimit = 3;
  let replyClicks = 0, lastReplyClickAt = options.yieldOnWait ? Math.max(0,
    ...Object.values(previousThreads).map(thread => Number.isFinite(thread.lastReplyClickAt) ? thread.lastReplyClickAt : 0)) : 0;
  let scrollRounds = 0, stalledScrollRounds = rootScrollProgress?.stalledRounds || 0, rootsInDomAtEnd = 0;
  let processed = 0, skippedEmpty = 0, explicitEnd = false;
  const checkAccess = () => {
    if (/website-login\/(?:captcha|error)|error_code=(?:300017|300031)/.test(location.href)) throw new Error('XHS_ACCESS_RESTRICTED');
    const actualNoteId = location.href.match(/^https:\/\/www\.xiaohongshu\.com\/(?:explore|search_result|user\/profile\/[a-f0-9]{24})\/([a-zA-Z0-9]{16,40})(?:[/?#]|$)/)?.[1];
    if (!expectedNoteId || actualNoteId !== expectedNoteId) {
      if (!/^https:\/\/www\.xiaohongshu\.com\/(?:chat|explore|search_result|user\/profile)(?:[/?#]|$)/.test(location.href)) throw new Error('ACCESS_STATE_UNCONFIRMED');
      const error = new Error('NOTE_SCOPE_CHANGED');
      error.code = 'NOTE_SCOPE_CHANGED';
      // Only already-captured public-note material is preserved. Never inspect
      // the new page's DOM, title, author, or contents after a scope change.
      error.partial = {results: [...rows.values()], coverage: {
        threads: [...threads.values()], observedRootIds: [...previousObserved],
        rootPaginationState: 'pending', scopeChanged: true,
        rootsRequested: options.roots, rootsProcessed: processed, replyClicks,
        completeness: 'interrupted_before_scope_exit_no_new_page_read'
      }};
      throw error;
    }
    if (options.accountUid) {
      const me = Array.from(document.querySelectorAll('a[href*="/user/profile/"]')).find(e => e.innerText?.trim() === '我');
      const uid = me?.href?.match(/\/user\/profile\/([a-f0-9]{24})/)?.[1];
      if (uid !== options.accountUid) throw new Error('ACCOUNT_MISMATCH');
    }
    if (/website-login\/(?:captcha|error)/.test(location.href) ||
        /请求太频繁|访问频次异常|安全限制|访问链接异常|登录后查看|登录(?:后)?(?:查看|浏览|阅读)(?:全部|更多)?(?:评论|内容)|登录查看更多|登录即可查看|请登录|Scan with logged-in|Log(?:in| in) to (?:view|read)|Sign in to (?:view|read)/i.test(document.body?.innerText || '')) {
      throw new Error('XHS_ACCESS_RESTRICTED');
    }
  };
  const hash = text => {
    let h = 2166136261;
    for (let i = 0; i < text.length; i++) h = Math.imul(h ^ text.charCodeAt(i), 16777619);
    return (h >>> 0).toString(16);
  };
  const domId = element => {
    if (!element) return '';
    for (const attr of ['data-comment-id', 'data-commentid', 'data-id', 'id']) {
      const value = element.getAttribute?.(attr) || '';
      const match = String(value).match(/^(?:comment[-_])?([a-fA-F0-9]{16,40})$/);
      if (match) return match[1];
    }
    return '';
  };
  const observedTarget = element => {
    for (const attr of ['data-target-comment-id', 'data-reply-comment-id']) {
      const value = element?.getAttribute?.(attr) || '';
      if (/^[a-fA-F0-9]{16,40}$/.test(value)) return value;
    }
    return '';
  };
  const fingerprint = item => hash([
    extractAuthorHref(item).split('?')[0], clean(item.querySelector('.content, .note-text')),
    clean(item.querySelector('.date, .time')), ...extractImages(item)
  ].join('\u0000'));
  const replyNodes = p => Array.from(p.querySelectorAll('.reply-container .comment-item-sub, .sub-comment-list .comment-item'));
  const unsafeExpanderCounts = new WeakMap();
  const replyExpanders = p => {
    let rejected = 0;
    const all = Array.from(p.querySelectorAll('button, [role="button"], span, div')).filter(el => {
      if (!(el instanceof HTMLElement) || !el.getClientRects().length) return false;
      const text = clean(el);
      if (text.length > 28 || !/^(?:展开(?:\s*更多)?(?:\s*\d+\s*条)?\s*回复|更多回复|全部回复|查看(?:全部|更多|\d+条)?回复|共\s*\d+\s*条回复)$/.test(text)) return false;
      const action = ['data-action', 'aria-label', 'title'].map(name => el.getAttribute?.(name) || '').join(' ');
      if (el.tagName?.toLowerCase() === 'a' || el.getAttribute?.('href') ||
          el.closest?.('a[href], [role="link"], .author-wrapper, .user-name, .name, .avatar, .user-info, .interactions, .like, .follow') ||
          el.querySelector?.('a[href], [role="link"]') || /私信|聊天|关注|发消息|message|chat|follow/i.test(action)) {
        rejected++; return false;
      }
      return true;
    });
    unsafeExpanderCounts.set(p, rejected);
    return all.filter(el => !all.some(child => child !== el && el.contains(child)));
  };
  const endVisible = () => {
    const candidates = document.querySelectorAll('.comments-container .end-container, .comments-container .end, .comments-container .list-end, .comments-container .no-more');
    return Array.from(candidates).some(el => el.getClientRects?.().length && /没有更多|已显示全部|到底了|^\s*-?\s*THE END\s*-?\s*$/.test(clean(el)));
  };
  // A zero counter (including the interaction bar's "评论" placeholder) is
  // not evidence that the lazy comment list has rendered its empty state.
  // Preserve the actual sources separately from the decision to stop paging.
  const rendered = element => {
    if (!element?.getClientRects?.().length) return false;
    if (typeof element.checkVisibility === 'function' && !element.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return false;
    const style = typeof window.getComputedStyle === 'function' ? window.getComputedStyle(element) : null;
    return !style || (style.display !== 'none' && !['hidden', 'collapse'].includes(style.visibility) && style.opacity !== '0');
  };
  const countSource = selector => {
    const element = document.querySelector(selector);
    const raw = String(element?.textContent ?? '');
    return {selector, text: raw.slice(0, 512), textTruncated: raw.length > 512, visible: rendered(element)};
  };
  const countObservation = () => {
    const header = countSource('.comments-container .total');
    const interactionBar = countSource('.interact-container .chat-wrapper .count');
    const parsedTotal = parseXhsCommentTotal(
      header.visible && !header.textTruncated ? header.text : null,
      interactionBar.visible && !interactionBar.textTruncated ? interactionBar.text : null);
    const headerParsedTotal = parseXhsCommentTotal(header.visible && !header.textTruncated ? header.text : null, null);
    const interactionBarParsedTotal = parseXhsCommentTotal(null, interactionBar.visible && !interactionBar.textTruncated ? interactionBar.text : null);
    return {header, interactionBar, parsedTotal, headerParsedTotal, interactionBarParsedTotal,
      positiveTotalObserved: [headerParsedTotal, interactionBarParsedTotal].some(total => total > 0), zeroIsEndSignal: false};
  };
  const emptySelectors = ['.total', '.empty', '.empty-text', '.empty-state', '.empty-container',
    '.no-comment', '.no-comments', '.comment-empty', '.empty-comment']
    .map(selector => '.comments-container ' + selector).join(', ');
  const observedEmptyState = () => {
    const container = document.querySelector('.comments-container');
    // A separately rendered XHS no-comments component replaces the list on
    // empty notes. Accept only the observed direct structure, never page text.
    if (!container) {
      const detail = document.querySelector('.note-detail-mask,.note-detail-container,.note-container');
      const scroller = detail?.querySelector('.note-scroller');
      if (!rendered(scroller) || scroller.tagName !== 'DIV' || !scroller.classList?.contains('note-scroller')) return null;
      const selector = '.note-scroller > .comments-el > .no-comments > p.no-comments-text';
      const candidates = Array.from(scroller.querySelectorAll(':scope > .comments-el > .no-comments > p.no-comments-text'));
      if (candidates.length > 4) return null;
      const matches = candidates.filter(element => {
        const box = element.parentElement, owner = box?.parentElement;
        return element.tagName === 'P' && element.classList?.contains('no-comments-text') &&
          box?.tagName === 'DIV' && box.classList?.contains('no-comments') &&
          owner?.tagName === 'DIV' && owner.classList?.contains('comments-el') && owner.parentElement === scroller &&
          [element, box, owner].every(rendered) && !element.closest?.('.parent-comment,.comment-item,.comment-item-sub,.sub-comment-list,.reply-container,#detail-desc,#detail-title,.note-text,.desc') &&
          String(element.textContent ?? '').length <= 80 && /^这是一片荒地\s*点击评论$/.test(String(element.textContent ?? '').trim());
      });
      if (matches.length !== 1) return null;
      if (Array.from(scroller.querySelectorAll('.parent-comment,.comment-item,.comment-item-sub,.sub-comment-list,.reply-container')).some(rendered)) return null;
      const loadingSelectors = ['.loading', '.loading-container', '[aria-busy="true"]'];
      const loadingVisible = loadingSelectors.some(selector => scroller.matches?.(selector)) ||
        Array.from(scroller.querySelectorAll(loadingSelectors.join(','))).some(rendered);
      return {scope: 'rendered_comments_container_empty_state', variant: 'no_comments_component', selector,
        commentsContainerPresent: false, directParentChainVerified: true, outsideBodyAndComment: true,
        text: String(matches[0].textContent), visible: true, loadingVisible, observedAt: Date.now()};
    }
    if (!rendered(container)) return null;
    const loadingVisible = Array.from(container.querySelectorAll?.('.loading, .loading-container, [aria-busy="true"]') || []).some(rendered);
    for (const [index, element] of Array.from(document.querySelectorAll(emptySelectors)).entries()) {
      if (!rendered(element) || !container.contains(element) ||
          element.closest?.('.parent-comment, .comment-item, .comment-item-sub, .sub-comment-list, .reply-container')) continue;
      const text = String(element.textContent ?? '');
      if (text.length > 512 || !/^(?:暂无评论|暂时没有评论|还没有评论|没有评论)(?:[哦呀呢吧~～！!。,.，\s]*|[，,！!。]?快来(?:抢沙发|评论)(?:吧)?[~～！!。]*)$/.test(text.trim())) continue;
      return {scope: 'rendered_comments_container_empty_state', selector: emptySelectors, domOrdinal: index + 1,
        text, visible: true, loadingVisible, observedAt: Date.now()};
    }
    return null;
  };
  let initialCountObservation = null, latestCountObservation = null, emptyCommentState = null;
  const imageVisible = img => {
    if (!img.getClientRects?.().length) return false;
    if (typeof img.checkVisibility === 'function' && !img.checkVisibility({checkOpacity: true, checkVisibilityCSS: true})) return false;
    const style = typeof window.getComputedStyle === 'function' ? window.getComputedStyle(img) : null;
    return !style || (style.display !== 'none' && !['hidden', 'collapse'].includes(style.visibility) && style.opacity !== '0');
  };
  const renderedInlineImages = content => {
    const assets = Array.from(content?.querySelectorAll('img') || [])
      .filter(img => img.classList?.contains('note-content-emoji'))
      .map((img, index) => ({img, domOrdinal: index + 1}))
      .filter(({img}) => imageVisible(img))
      .map(({img, domOrdinal}) => ({domOrdinal, url: String(img.currentSrc || img.getAttribute?.('src') || ''),
        observedClass: 'note-content-emoji'}));
    // Even missing sources are evidence of unresolved media when there is text
    // or another usable asset. captureRow still rejects wholly missing bodies.
    return assets;
  };
  const renderedEmojiLabels = content => {
    const imgs = Array.from(content?.querySelectorAll('img') || []).filter(imageVisible);
    if (!imgs.length) return {text: '', labels: []};
    const labels = imgs.map(img => {
      for (const attribute of ['alt', 'aria-label', 'title']) {
        const text = String(img.getAttribute?.(attribute) || '').trim();
        if (text) return {attribute, text};
      }
      return {attribute: '', text: ''};
    });
    // Only actual rendered emoji labels qualify. Generic picture captions and
    // missing labels remain a media/content gap. Never infer from a URL/name.
    const emojiLabel = text => /^\[[^\]\r\n]{1,40}[RH]\]$/u.test(text) ||
      (/^(?:\p{Extended_Pictographic}|\p{Emoji_Modifier}|\p{Regional_Indicator}|\uFE0F|\u200D|[0-9#*]\uFE0F?\u20E3)+$/u.test(text) &&
       /\p{Extended_Pictographic}|\p{Regional_Indicator}|[0-9#*]\uFE0F?\u20E3/u.test(text));
    if (labels.some(label => !emojiLabel(label.text))) return {text: '', labels: []};
    return {text: labels.map(label => label.text).join(''), labels};
  };
  const replyPrefixOnly = (content, nickname) => {
    if (!nickname || nickname.parentElement !== content || !clean(nickname)) return false;
    const nodes = Array.from(content.childNodes || []);
    const index = nodes.indexOf(nickname);
    if (index < 0) return false;
    // Inspect the actual direct nickname wrapper. A user's literal sentence
    // resembling a reply prefix is never stripped by a text-only heuristic.
    const before = nodes.slice(0, index).map(node => node.textContent || '').join('').trim();
    const after = nodes.slice(index + 1).map(node => node.textContent || '').join('').trim();
    return before === '回复' && /^[:：]$/.test(after);
  };
  const publicAuthorId = item => {
    const href = extractAuthorHref(item).split(/[?#]/)[0];
    return href.match(/^(?:https:\/\/www\.xiaohongshu\.com)?\/user\/profile\/([a-fA-F0-9]{16,40})\/?$/)?.[1] || '';
  };
  const captureRow = (item, root, isReply, index) => {
    const images = extractImages(item);
    const content = item.querySelector('.content, .note-text');
    const rawText = clean(content);
    const nickname = isReply ? content?.querySelector(':scope > .nickname') : null;
    const targetName = clean(nickname);
    const prefixOnly = isReply && replyPrefixOnly(content, nickname);
    const bodyText = prefixOnly ? '' : rawText;
    const emoji = !bodyText && !images.length ? renderedEmojiLabels(content) : {text: '', labels: []};
    // Complete accessible labels are already captured above as actual text.
    // Other inline assets are omitted by ordinary pictures and textContent;
    // keep their observed URLs/occurrences, never infer their meaning.
    const inlineImages = emoji.text ? [] : renderedInlineImages(content);
    const capturedText = bodyText || emoji.text;
    const commentId = domId(item) || (!isReply ? root.commentId : '');
    const authorId = publicAuthorId(item);
    if (!capturedText && !images.length && !inlineImages.some(image => image.url)) {
      if (item.querySelector('.content img[src], .note-text img[src]')) skippedEmpty++;
      if (isReply) {
        const evidence = commentId && root.replyBodyCaptures?.[commentId];
        const retained = !!(evidence && authorId && evidence.authorId === authorId &&
          evidence.commentId === commentId && evidence.parentCommentId === root.commentId && evidence.text);
        const gap = {commentId, replyIndex: index, reply_to: targetName || root.rootAuthor,
          renderedText: rawText, renderedReplyPrefixOnly: prefixOnly,
          reason: 'reply_content_not_rendered', retainedPriorBody: retained};
        root.currentReplyBodyGaps.push(gap);
        if (!retained) root.replyBodyGaps.push(gap);
        // Do not emit a prefix-only row that could replace a prior real body.
        return retained;
      }
      return false;
    }
    const author = clean(item.querySelector('.author-wrapper .name, .name, .user-name'));
    const sourceFingerprint = fingerprint(item);
    // Fingerprints are navigation aids, never claimed to be platform IDs.
    const sourceKey = commentId ? 'id:' + commentId : root.rootKey + ':' + (isReply ? 'reply:' + index : 'root') + ':' + sourceFingerprint;
    const row = {
      author, authorHrefRaw: extractAuthorHref(item), text: capturedText || '[图片评论]',
      textSource: bodyText ? 'rendered_text' : emoji.text ?
        (emoji.labels.every(label => label.attribute === 'alt') ? 'rendered_emoji_alt' : 'rendered_emoji_accessible_label') : 'media_placeholder',
      ...(emoji.text ? {renderedImageLabels: emoji.labels} : {}),
      ...(inlineImages.length ? {renderedInlineImages: inlineImages} : {}),
      ...(prefixOnly ? {renderedText: rawText, renderedReplyPrefixOnly: true} : {}),
      likes: parseLikes(item.querySelector('.count')), time: clean(item.querySelector('.date, .time')),
      is_reply: isReply, reply_to: isReply ? targetName || root.rootAuthor : '', images,
      commentId, parentCommentId: isReply ? root.commentId : '',
      replyToCommentId: isReply ? observedTarget(item) || (!targetName ? root.commentId : '') : '',
      replyToIdentity: !isReply ? 'none' : observedTarget(item) ? 'observed_id' : targetName ? 'display_name_only' : 'thread_root',
      rootKey: root.rootKey, sourceFingerprint, identityKind: commentId ? 'observed_dom_id' : 'uncertain_fingerprint', sourceKey
    };
    rows.set(sourceKey, row);
    if (isReply && commentId && root.commentId && authorId && (capturedText || images.length || inlineImages.length)) {
      root.replyBodyCaptures[commentId] = {commentId, parentCommentId: root.commentId, authorId,
        text: row.text, textSource: row.textSource, ...(images.length ? {images} : {}),
        ...(inlineImages.length ? {renderedInlineImages: inlineImages} : {})};
    }
    return true;
  };
  const completionGap = thread => {
    if (thread.pendingReplyExpansion) return thread.pendingReplyExpansion.state === 'outcome_unconfirmed'
      ? 'reply_expansion_outcome_unconfirmed' : 'reply_render_pending';
    if (thread.unsafeExpanders) return 'unsafe_expander_not_clicked';
    if (!thread.commentId) return 'root_identity_unconfirmed';
    if (!thread.contentCaptured) return 'root_content_not_rendered';
    if (!thread.replyContentCaptured) return 'reply_content_not_rendered';
    if (!thread.replyIdentityComplete) return 'reply_identity_unconfirmed';
    if (!Number.isInteger(thread.expectedReplies) || thread.expectedReplies < 0) {
      if (explicitEnd && !thread.expanders.length) {thread.completionBasis='rendered_scope_end_no_expander_reply_total_unknown';return '';}
      return 'expected_reply_count_unknown';
    }
    if (thread.uniqueObservedReplyCount < thread.expectedReplies) return 'expected_replies_not_visible';
    return '';
  };
  const realId = value => typeof value === 'string' && /^[a-fA-F0-9]{16,40}$/.test(value);
  const knownReplyIds = thread => !realId(thread?.commentId) ||
    thread.rootKey && thread.rootKey !== 'id:' + thread.commentId ? new Set() : new Set([
      ...Object.entries(thread?.replyBodyCaptures || {}).filter(([id, body]) =>
        body?.commentId === id && body?.parentCommentId === thread.commentId).map(([id]) => id),
      ...(Array.isArray(thread?.lastReplyExpansion?.observedReplyIds) ? thread.lastReplyExpansion.observedReplyIds : [])
    ].filter(realId));
  const reliableReplyBody = (id, body, rootId) => !!(realId(id) && realId(rootId) &&
    body?.commentId === id && body.parentCommentId === rootId && realId(body.authorId) &&
    typeof body.text === 'string' && body.text.trim() && body.text !== '[图片评论]' &&
    !/^(?:原|该|此)?评论已(?:被)?删除[。.!！]?$/.test(body.text.trim().replace(/\s+/g, '')) &&
    !/^回复\s*.+?\s*[:：]$/.test(body.text.trim()) &&
    ['rendered_text', 'rendered_emoji_alt', 'rendered_emoji_accessible_label'].includes(body.textSource));
  const changedReplyBody = (id, before, current, rootId) => !!(before && current &&
    current.commentId === id && before.commentId === id && current.parentCommentId === rootId &&
    before.parentCommentId === rootId && current.authorId && current.authorId === before.authorId &&
    current.text && current.textSource !== 'media_placeholder' && current.text !== before.text);
  const replacementDocumentEvidence = (parent, thread, pending) => {
    const old = previousThreads[thread.rootKey];
    if (!documentTimeOrigin || !Number.isFinite(pending?.clickedAt) || pending.clickedAt <= 0 ||
        documentTimeOrigin <= pending.clickedAt || parent.isConnected === false ||
        !thread.commentId || thread.rootKey !== 'id:' + thread.commentId || old?.commentId !== thread.commentId ||
        (domId(parent.querySelector('.comment-item')) || domId(parent)) !== thread.commentId ||
        !thread.contentCaptured || !thread.replyContentCaptured || !thread.replyIdentityComplete) return null;
    const priorOrigin = pending.documentTimeOrigin;
    if (priorOrigin !== undefined && priorOrigin !== null &&
        (!Number.isFinite(priorOrigin) || priorOrigin <= 0 || priorOrigin === documentTimeOrigin || priorOrigin > pending.clickedAt)) return null;
    const controls = replyExpanders(parent);
    if (!controls.length) return null;
    return {basis: 'current_document_created_after_original_click', noteId: expectedNoteId,
      rootCommentId: thread.commentId, originalClickedAt: pending.clickedAt,
      previousDocumentTimeOrigin: priorOrigin ?? null, currentDocumentTimeOrigin: documentTimeOrigin,
      observedAt: Date.now(), strictExpanderText: clean(controls[0])};
  };
  const scan = () => {
    checkAccess();
    if (!withinBudget()) handoff('time_budget_pending');
    const counts = new Map();
    const found = [];
    const parents = Array.from(document.querySelectorAll('.parent-comment'));
    rootsInDomAtEnd = parents.length;
    latestCountObservation = countObservation();
    if (!initialCountObservation) initialCountObservation = latestCountObservation;
    emptyCommentState = observedEmptyState();
    if (options.yieldOnWait) explicitEnd = explicitEnd || endVisible();
    // After a bounded scan, undiscovered/unfinished roots get a chance before
    // re-reading the same prefix. This is only a visit order, never dedup proof.
    if (options.yieldOnWait) parents.sort((a, b) => {
      const priority = p => {
        const id = domId(p.querySelector('.comment-item')) || domId(p);
        const old = id && previousThreads['id:' + id];
        return !old || old.scanIncomplete ? 0 : 1;
      };
      return priority(a) - priority(b);
    });
    for (const p of parents) {
      if (!withinBudget()) handoff('time_budget_pending');
      const item = p.querySelector('.comment-item');
      if (!item) continue;
      const commentId = domId(item) || domId(p);
      if (replyScope && !replyRootIds.has(commentId)) {outsideReplyScope.add(commentId || 'identity_unconfirmed'); continue;}
      const fp = fingerprint(item);
      const occurrence = counts.get(fp) || 0;
      counts.set(fp, occurrence + 1);
      const rootKey = commentId ? 'id:' + commentId : 'fp:' + fp + ':' + occurrence;
      const existing = threads.get(rootKey);
      const old = previousThreads[rootKey];
      const thread = existing || {
        rootKey, commentId, identityKind: commentId ? 'observed_dom_id' : 'uncertain_fingerprint',
        rootAuthor: clean(item.querySelector('.author-wrapper .name, .name, .user-name')),
        rootText: clean(item.querySelector('.content, .note-text')).slice(0, 180),
        sourceFingerprint: fp, processed: false,
        replyBodyCaptures: {...(old?.replyBodyCaptures || {})},
        replyBodyMinimumCount: old?.replyBodyMinimumCount || 0,
        legacyReplyBodyIds: old?.legacyReplyBodyIds || [],
        ...(old?.lastReplyClickAt ? {lastReplyClickAt: old.lastReplyClickAt} : {}),
        ...(old?.pendingReplyExpansion ? {pendingReplyExpansion: {...old.pendingReplyExpansion}} : {}),
        ...(old?.lastReplyExpansion ? {lastReplyExpansion: {...old.lastReplyExpansion}} : {}),
        ...(old?.replyDiscoveryDeferred ? {replyDiscoveryDeferred: {...old.replyDiscoveryDeferred}} : {}),
        ...(old?.discoveredReplyVisit ? {discoveredReplyVisit: {...old.discoveredReplyVisit}} : {}),
        ...(Array.isArray(old?.retiredReplyExpansions) ? {retiredReplyExpansions: [...old.retiredReplyExpansions]} : {}),
        state: old?.state === 'visible_replies_read' ? 'previously_read' : old?.state === 'visible_scope_pending_end' && old.sourceFingerprint===fp ? 'visible_scope_pending_end' : old?.state === 'stalled' && prior.retryStalled === false ? 'stalled' : 'not_processed'
      };
      if (thread.state === 'previously_read' && Number.isInteger(prior.expectedReplyCounts?.[commentId])
          && prior.expectedReplyCounts[commentId] > (old?.visibleReplies || 0)) thread.state = 'not_processed';
      const replies = replyNodes(p);
      thread.visibleReplies = replies.length;
      thread.expanders = replyExpanders(p).map(clean);
      thread.unsafeExpanders = unsafeExpanderCounts.get(p) || 0;
      threads.set(rootKey, thread);
      if (commentId) previousObserved.add(commentId);
      seenInPass.add(rootKey);
      thread.contentCaptured = captureRow(item, thread, false, 0);
      thread.replyBodyGaps = [];
      thread.currentReplyBodyGaps = [];
      thread.scanIncomplete = true;
      const capturedReplies = [], replyIds = [];
      for (let i = 0; i < replies.length; i++) {
        if (!withinBudget()) {
          thread.replyContentCaptured = false;
          thread.replyIdentityComplete = false;
          handoff('time_budget_pending', thread);
        }
        capturedReplies.push(captureRow(replies[i], thread, true, i));
        replyIds.push(domId(replies[i]));
      }
      if (!withinBudget()) {
        thread.replyContentCaptured = false;
        thread.replyIdentityComplete = false;
        handoff('time_budget_pending', thread);
      }
      thread.scanIncomplete = false;
      thread.uniqueObservedReplyCount = new Set(replyIds.filter(Boolean)).size;
      thread.replyContentCaptured = capturedReplies.every(Boolean);
      const missingPriorIds = thread.legacyReplyBodyIds.filter(id => !thread.replyBodyCaptures[id]);
      thread.replyBodyVerificationPending = Object.keys(thread.replyBodyCaptures).length < thread.replyBodyMinimumCount || !!missingPriorIds.length;
      if (thread.replyBodyVerificationPending) {
        thread.replyContentCaptured = false;
        thread.replyBodyGaps.push({reason: 'prior_reply_body_unverified', missingCommentIds: missingPriorIds,
          minimumPreviouslyObserved: thread.replyBodyMinimumCount, verifiedBodyCount: Object.keys(thread.replyBodyCaptures).length});
      }
      thread.replyIdentityComplete = replyIds.every(Boolean);
      const knownIds = knownReplyIds(old);
      thread.replyEvidenceObservation = {
        newReplyIds: replyIds.filter(id => id && !knownIds.has(id)),
        changedVerifiedBodyIds: replyIds.filter(id => changedReplyBody(id,
          old?.replyBodyCaptures?.[id], thread.replyBodyCaptures[id], thread.commentId)),
        restoredKnownReplyIds: replyIds.filter(id => id && knownIds.has(id)),
      };
      if (replyScope && thread.discoveredReplyVisit?.scopeId === replyScope.scopeId &&
          (thread.replyEvidenceObservation.newReplyIds.length || thread.replyEvidenceObservation.changedVerifiedBodyIds.length)) {
        thread.discoveredReplyVisit = {...thread.discoveredReplyVisit, state: 'new_visible_evidence_pending', reopenedAt: Date.now()};
        replyVisits.delete(thread.rootKey);
      }
      const expected = prior.expectedReplyCounts?.[commentId];
      thread.expectedReplies = Number.isInteger(expected) ? expected : null;
      if (options.yieldOnWait && thread.pendingReplyExpansion) {
        const pending = thread.pendingReplyExpansion;
        const baselineBodies = {...(old?.replyBodyCaptures || {}), ...(pending.beforeKnownReplyBodyCaptures || {}),
          ...(pending.beforeReplyBodyCaptures || {})};
        const beforeIds = new Set([...(pending.beforeReplyIds || []),
          ...(pending.beforeKnownReplyIds || []), ...knownIds]);
        const newId = Array.isArray(pending.beforeReplyIds) && replyIds.some(id => id && !beforeIds.has(id));
        const newReliableBody = replyIds.some(id => changedReplyBody(id,
          baselineBodies[id], thread.replyBodyCaptures[id], thread.commentId) &&
          (!old?.replyBodyCaptures?.[id] || changedReplyBody(id,
            old.replyBodyCaptures[id], thread.replyBodyCaptures[id], thread.commentId)));
        const restoredIds = pending.noteId === expectedNoteId && pending.rootCommentId === thread.commentId &&
          Array.isArray(pending.beforeKnownReplyIds) ? replyIds.filter(id => {
            const before = pending.beforeKnownReplyBodyCaptures?.[id], current = thread.replyBodyCaptures[id];
            return id && !(pending.beforeReplyIds || []).includes(id) && pending.beforeKnownReplyIds.includes(id) &&
              reliableReplyBody(id, before, thread.commentId) && reliableReplyBody(id, current, thread.commentId) &&
              before.authorId === current.authorId && before.text === current.text && before.textSource === current.textSource;
          }) : [];
        if (newId || newReliableBody || restoredIds.length) {
          thread.lastReplyExpansion = {...pending, state: 'render_progress_observed', observedAt: Date.now(),
            observedVisibleReplies: replies.length, observedReplyIds: replyIds.filter(Boolean),
            progressBasis: newId ? 'new_rendered_reply_id' : newReliableBody ? 'changed_verified_reply_body' : 'known_reply_render_restored',
            newResearchEvidence: !!(newId || newReliableBody),
            ...(restoredIds.length ? {restoredReplyIds: restoredIds} : {})};
          delete thread.pendingReplyExpansion;
          thread.state = 'not_processed';
        } else if (replacementDocumentEvidence(p, thread, pending)) {
          // This is only readiness to re-expand a genuinely different document.
          // Keep the old intent until a fresh strict control is checked at click.
          thread.state = 'not_processed';
          thread.reason = 'replacement_document_reexpansion_pending';
          thread.processed = false;
        } else {
          const reappearedIds = replyIds.filter(id => id && beforeIds.has(id) && !(pending.beforeReplyIds || []).includes(id));
          if (reappearedIds.length) pending.lastRestorationObservation = {state: 'known_reply_render_without_preclick_body_proof',
            observedAt: Date.now(), replyIds: reappearedIds, newResearchEvidence: false};
          // Count one completed DOM observation per batch, not repeated scans
          // of the same synchronous view. Elapsed time alone never releases a
          // submitted click or authorises clicking that unknown outcome again.
          if (!pendingObservedThisBatch.has(thread.rootKey)) {
            pendingObservedThisBatch.add(thread.rootKey);
            pending.pendingObservations = (Number.isInteger(pending.pendingObservations) && pending.pendingObservations >= 0
              ? pending.pendingObservations : 0) + 1;
            pending.lastUnchangedObservationAt = Date.now();
          }
          if (pending.pendingObservations >= pendingObservationLimit) {
            const alreadyIsolated = pending.state === 'outcome_unconfirmed';
            if (pending.state !== 'outcome_unconfirmed') pending.originalSubmissionState = pending.state;
            pending.state = 'outcome_unconfirmed';
            pending.diagnostic = {reason: 'rendered_reply_ids_and_verified_bodies_unchanged',
              observationLimit: pendingObservationLimit, unchangedObservations: pending.pendingObservations,
              lastObservedAt: pending.lastUnchangedObservationAt, retryPermitted: false};
            thread.state = 'stalled';
            thread.reason = 'reply_expansion_outcome_unconfirmed';
            if (!thread.processed) {
              thread.processed = true;
              // Diagnose once. An already isolated floor must not consume
              // every small subsequent batch's root budget again.
              if (!alreadyIsolated && processed < options.roots) processed++;
            }
          } else {
            thread.state = 'pending';
            thread.reason = 'reply_render_pending';
          }
        }
      }
      if (thread.state === 'previously_read' && thread.replyBodyGaps.length) {
        // Old boolean completion is not proof that this reply's body was read.
        thread.state = 'stalled';
        thread.reason = 'reply_content_not_rendered';
        thread.reopenedBy = 'observed_reply_body_gap';
      }
      // A late render may finish after expand() classified a short snapshot.
      // Re-evaluate that stall from captured real IDs, never just row counts or
      // the absence of an expander. Uncertain identities remain unresolved.
      if(thread.state==='visible_scope_pending_end' && (thread.expanders.length || !['','expected_reply_count_unknown'].includes(completionGap(thread))))thread.state='not_processed';
      if (['stalled','visible_scope_pending_end'].includes(thread.state) && !thread.expanders.length && !completionGap(thread)) {
        thread.state = 'visible_replies_read';
        thread.reason = '';
        thread.resolvedBy = 'captured_reply_ids_after_stall';
      }
      // A large root scan may use the whole budget before expand() is reached.
      // Finalize only already captured, non-expandable roots within the same
      // root budget, so repeated bounded scans can advance beyond their prefix.
      if (options.yieldOnWait && !thread.processed && thread.state === 'not_processed' &&
          !thread.expanders.length && !thread.pendingReplyExpansion && processed < options.roots) {
        const gap = completionGap(thread);
        thread.processed = true;
        thread.state = gap === 'expected_reply_count_unknown' ? 'visible_scope_pending_end'
          : gap ? 'stalled' : 'visible_replies_read';
        thread.reason = gap;
        processed++;
      }
      if (replyScope && thread.discoveredReplyVisit?.scopeId === replyScope.scopeId &&
          (thread.pendingReplyExpansion || !thread.contentCaptured || !thread.replyContentCaptured ||
           !thread.replyIdentityComplete || thread.replyBodyVerificationPending)) {
        thread.discoveredReplyVisit = {...thread.discoveredReplyVisit, state: 'current_capture_unconfirmed', reopenedAt: Date.now()};
        replyVisits.delete(thread.rootKey);
      }
      if (replyScope && thread.contentCaptured && thread.replyContentCaptured && thread.replyIdentityComplete &&
          !thread.replyBodyVerificationPending && !thread.pendingReplyExpansion && !thread.expanders.length &&
          !thread.unsafeExpanders && !thread.scanIncomplete && !thread.identityAmbiguous &&
          thread.uniqueObservedReplyCount >= knownReplyIds(thread).size) {
        if (!replyVisits.has(thread.rootKey)) thread.discoveredReplyVisit = {
          scopeId: replyScope.scopeId, rootCommentId: thread.commentId, state: 'captured_visible_scope',
          capturedAt: Date.now(), documentTimeOrigin, observedReplyIds: replyIds.filter(Boolean),
          semanticReviewed: false, mediaReviewed: false, parentEndRequiredForCompletion: true};
        replyVisits.set(thread.rootKey, thread.discoveredReplyVisit);
      }
      found.push({p, item, thread});
    }
    // Duplicate fingerprint matches cannot be safely resumed after a reorder.
    for (const {thread} of found) if (!thread.commentId && counts.get(thread.sourceFingerprint) > 1) {
      thread.identityAmbiguous = true;
      if (thread.state === 'previously_read') thread.state = 'not_processed';
    }
    if (!withinBudget()) handoff('time_budget_pending');
    explicitEnd = explicitEnd || endVisible();
    return found;
  };
  const expand = async ({p, thread}) => {
    let stalled = 0;
    if (options.yieldOnWait && thread.pendingReplyExpansion &&
        !replacementDocumentEvidence(p, thread, thread.pendingReplyExpansion)) {
      handoff('reply_render_pending', thread, 400);
    }
    while (withinBudget() && replyClicks < options.maxReplyClicks) {
      checkAccess();
      const more = replyExpanders(p);
      if (!more.length) break;
      const pause = 3000 - (Date.now() - lastReplyClickAt);
      if (pause > 0) await wait(pause, 'reply_click_pacing_pending', thread);
      if (!withinBudget()) break;
      const before = replyNodes(p).length;
      more[0].scrollIntoView({block: 'center'});
      checkAccess();
      let replacement = null;
      if (options.yieldOnWait && thread.pendingReplyExpansion) {
        replacement = replacementDocumentEvidence(p, thread, thread.pendingReplyExpansion);
        if (!replacement || replyExpanders(p)[0] !== more[0]) handoff('reply_render_pending', thread, 400);
        thread.retiredReplyExpansions = [...(thread.retiredReplyExpansions || []),
          {...thread.pendingReplyExpansion, originalState: thread.pendingReplyExpansion.state,
            state: 'retired_by_replacement_document', retiredAt: Date.now(),
            replacementDocument: replacement}];
        delete thread.pendingReplyExpansion;
      }
      // Save the intent before submitting it. A later batch does not click the
      // same expander again until actual rendered reply progress is observed.
      const beforeReplyIds = replyNodes(p).map(domId).filter(Boolean);
      if (options.yieldOnWait) thread.pendingReplyExpansion = {state: 'click_intent_recorded',
        noteId: expectedNoteId, rootCommentId: thread.commentId,
        clickedAt: Date.now(), beforeVisibleReplies: before,
        documentTimeOrigin,
        ...(replacement ? {replacesRetiredIntent: thread.retiredReplyExpansions.length - 1,
          replacementDocument: replacement} : {}),
        beforeReplyIds,
        beforeKnownReplyIds: [...knownReplyIds(thread)],
        beforeKnownReplyBodyCaptures: Object.fromEntries(Object.entries(thread.replyBodyCaptures)
          .filter(([id, body]) => !beforeReplyIds.includes(id) && reliableReplyBody(id, body, thread.commentId))
          .map(([id, body]) => [id, {...body}])),
        beforeReplyBodyCaptures: Object.fromEntries(replyNodes(p).map(domId).filter(id => id && thread.replyBodyCaptures[id])
          .map(id => [id, {...thread.replyBodyCaptures[id]}])),
        expanderText: clean(more[0]), pendingObservations: 0};
      thread.lastReplyClickAt = lastReplyClickAt = Date.now();
      more[0].click(); replyClicks++;
      if (options.yieldOnWait) {
        thread.pendingReplyExpansion.state = 'click_submitted';
        scan();
      }
      // Preserve rows even when virtualization replaces earlier DOM elements.
      for (let poll = 0; poll < 8 && withinBudget(); poll++) {
        await wait(400, 'reply_render_pending', thread); scan();
        if (replyNodes(p).length > before || !replyExpanders(p).length) break;
      }
      if (replyNodes(p).length > before) stalled = 0;
      else if (++stalled >= 3) break;
    }
    // Reacquire and capture the final rendered root. An old node's row count is
    // not evidence that unique, identified replies were actually saved.
    if (!withinBudget()) handoff('time_budget_pending', thread);
    const current = scan().find(found => found.thread.rootKey === thread.rootKey);
    thread.processed = true;
    const gap = current ? completionGap(thread) : 'root_not_rendered_after_expansion';
    thread.state = !current || !thread.contentCaptured || !thread.replyContentCaptured || thread.unsafeExpanders ? 'stalled'
      : thread.expanders.length ? (stalled >= 3 ? 'stalled' : 'pending')
      : gap === 'expected_reply_count_unknown' ? 'visible_scope_pending_end' : gap ? 'stalled' : 'visible_replies_read';
    thread.reason = gap || (stalled >= 3 ? 'reply_expansion_stalled' : '');
    processed++;
  };
  const allExpectedObserved = () => prior.apiRootsComplete === true && [...expectedIds].every(id => previousObserved.has(id));
  const outstandingVisited = () => [...needsVisit].every(key => seenInPass.has(key));
  const confirmedEmpty = () => !!emptyCommentState && !emptyCommentState.loadingVisible &&
    !latestCountObservation?.positiveTotalObserved && rootsInDomAtEnd === 0 && !threads.size &&
    !Object.keys(previousThreads).length && !expectedIds.size && !previousObserved.size && !seenInPass.size && !needsVisit.size;
  const settleRootScroll = visible => {
    if (!rootScrollProgress) return;
    const pending = rootScrollProgress.pendingIntent;
    const baseline = new Set(pending?.beforeObservedRootIds || prior.observedRootIds || []);
    const newIds = visible.map(({thread}) => thread.commentId).filter(id => id && !baseline.has(id));
    const endObserved = confirmedEmpty() || explicitEnd || (allExpectedObserved() && outstandingVisited());
    if (newIds.length || endObserved) {
      rootScrollProgress.stalledRounds = stalledScrollRounds = 0;
      rootScrollProgress.state = endObserved ? 'end_observed' : 'ready';
      if (pending || newIds.length || rootScrollProgress.lastObservation?.state !== 'end_observed')
        rootScrollProgress.lastObservation = {state: endObserved ? 'end_observed' : 'new_root_ids_observed',
          observedAt: Date.now(), newRootIds: [...new Set(newIds)], attempt: pending?.attempt || null};
      rootScrollProgress.pendingIntent = null;
    } else if (pending) {
      const remainingWait = (pending.minimumWaitMs || 400) - (Date.now() - pending.submittedAt);
      if (remainingWait > 0) handoff('root_render_pending', null, remainingWait);
      // A submitted scroll is settled exactly once, on a later real snapshot.
      // No new IDs is a bounded stall observation, never platform-end evidence.
      rootScrollProgress.stalledRounds++;
      stalledScrollRounds = rootScrollProgress.stalledRounds;
      rootScrollProgress.pendingIntent = null;
      rootScrollProgress.state = stalledScrollRounds >= 3 ? 'stalled' : 'ready';
      rootScrollProgress.lastObservation = {state: 'no_new_observed_root_ids', observedAt: Date.now(),
        attempt: pending.attempt, baselineRootIds: pending.beforeObservedRootIds, stalledRounds: stalledScrollRounds};
    }
    if (rootScrollProgress.state === 'stalled') {
      rootScrollProgress.lastSelection = {...selectRootScroller().diagnostic,
        outcome: 'diagnostic_only_stalled_no_action'};
      if (rootRepairProbe?.allowScroll && repairScrolls === 0) return;
      handoff('root_pagination_stalled');
    }
  };
  const scrollMetrics = element => {
    const rect = element?.getBoundingClientRect?.();
    const finite = value => Number.isFinite(value) ? value : null;
    const viewportWidth = finite(window.innerWidth), viewportHeight = finite(window.innerHeight);
    return {tagName: element?.tagName || '', className: String(element?.className || '').slice(0, 160),
      scrollTop: finite(element?.scrollTop), scrollHeight: finite(element?.scrollHeight), clientHeight: finite(element?.clientHeight),
      clientWidth: finite(element?.clientWidth), viewportWidth, viewportHeight,
      rect: rect ? {top: finite(rect.top), left: finite(rect.left), width: finite(rect.width), height: finite(rect.height),
        bottom: finite(rect.bottom), right: finite(rect.right)} : null,
      visibleWidth: rect && viewportWidth !== null ? Math.max(0, Math.min(rect.right, viewportWidth) - Math.max(rect.left, 0)) : null,
      visibleHeight: rect && viewportHeight !== null ? Math.max(0, Math.min(rect.bottom, viewportHeight) - Math.max(rect.top, 0)) : null};
  };
  const selectRootScroller = () => {
    const candidates = [], visited = new Set();
    const noteScope = document.querySelector('#noteContainer, .note-detail-mask, .note-detail-container, .note-container');
    const inspect = (element, relation, scopeVerified) => {
      if (!element || visited.has(element) || candidates.length >= 12) return null;
      visited.add(element);
      const metrics = scrollMetrics(element);
      const style = typeof window.getComputedStyle === 'function' ? window.getComputedStyle(element) : null;
      const isDocument = element === document.scrollingElement;
      const dimensions = metrics.clientWidth > 0 && metrics.clientHeight > 0 &&
        metrics.viewportWidth > 0 && metrics.viewportHeight > 0 && metrics.rect?.width > 0 && metrics.rect?.height > 0 &&
        metrics.visibleWidth > 0 && metrics.visibleHeight > 0;
      const visible = dimensions && (!style || style.display !== 'none' &&
        !['hidden', 'collapse'].includes(style.visibility) && style.opacity !== '0');
      const range = metrics.scrollHeight > metrics.clientHeight + 10;
      const eligible = !!(scopeVerified && visible && range && (isDocument || /auto|scroll/.test(style?.overflowY || '')));
      candidates.push({...metrics, relation, scopeVerified: !!scopeVerified, isDocument,
        overflowY: style?.overflowY || '', eligible,
        rejectedReason: eligible ? '' : !scopeVerified ? 'outside_observed_note_scope' : !visible ? 'size_or_visibility_unconfirmed'
          : !range ? 'no_scroll_range' : 'overflow_not_scrollable'});
      return eligible ? {element, basis: relation} : null;
    };
    let ancestor = document.querySelector('.comments-container');
    const commentsInNote = !noteScope || !!noteScope.contains(ancestor);
    let selected = null;
    for (let depth = 0; ancestor && depth < 6; depth++, ancestor = ancestor.parentElement) {
      selected = inspect(ancestor, 'comments_ancestor', commentsInNote);
      if (selected) break;
    }
    if (!selected) for (const element of Array.from(document.querySelectorAll('.note-scroller')).slice(0, 4)) {
      selected = inspect(element, 'active_note_scroller', !!noteScope?.contains(element));
      if (selected) break;
    }
    if (!selected) selected = inspect(scroller, 'verified_legacy_note_scroller', !!noteScope?.contains(scroller));
    if (!selected) selected = inspect(document.scrollingElement, 'document_scrolling_element',
      !!noteScope && !!document.scrollingElement?.contains(noteScope));
    return {element: selected?.element || null, diagnostic: {at: Date.now(),
      outcome: selected ? 'verified_scroll_container_selected' : 'no_verified_scroll_container',
      selectionBasis: selected?.basis || null, candidates,
      selectedBefore: selected ? scrollMetrics(selected.element) : null}};
  };
  try { while (withinBudget()) {
    const visible = scan();
    if (!replyScope) settleRootScroll(visible);
    if (rootRepairProbe) {
      if (visible.some(({thread}) => thread.commentId && !probeBaselineRootIds.has(thread.commentId)) ||
          confirmedEmpty() || explicitEnd || allExpectedObserved() && outstandingVisited()) break;
      if (!rootRepairProbe.allowScroll || repairScrolls >= 1) handoff('root_repair_observation_only');
    } else {
      if (replyScope && [...replyRootIds].every(id => replyVisits.has('id:' + id))) {
        replyScopeState = 'known_parent_visit_pass_finished';
        break; // A bounded capture pass ends as a gap, never as global END.
      }
      // A different doctor's task can replace this document and fold its old
      // replies. Reopening that already-read prefix on every return must not
      // starve discovery of later parents. Keep all reply gaps and intentions;
      // a genuine parent END (or complete API scope) returns them to expansion.
      const deferKnownReplyRestoration = ({p, thread}) => {
        if (replyScope || !options.yieldOnWait || explicitEnd || allExpectedObserved() && outstandingVisited()) return false;
        const old = previousThreads[thread.rootKey], observed = thread.replyEvidenceObservation;
        if (!old || old.commentId !== thread.commentId || !thread.commentId ||
            !old.contentCaptured || !thread.contentCaptured || !knownReplyIds(old).size ||
            observed.newReplyIds.length || observed.changedVerifiedBodyIds.length) return false;
        const continuedDeferral = old.replyDiscoveryDeferred?.noteId === expectedNoteId &&
          old.replyDiscoveryDeferred?.rootCommentId === thread.commentId;
        if (!['visible_scope_pending_end', 'visible_replies_read', 'previously_read'].includes(old.state) &&
            !continuedDeferral && !replacementDocumentEvidence(p, thread, thread.pendingReplyExpansion)) return false;
        thread.replyDiscoveryDeferred = {noteId: expectedNoteId, rootCommentId: thread.commentId,
          reason: 'known_reply_restoration_deferred_for_parent_discovery', observedAt: Date.now()};
        discoveryDeferredReplyRoots.set(thread.rootKey, {rootKey: thread.rootKey, commentId: thread.commentId,
          reason: 'known_reply_restoration_deferred_for_parent_discovery',
          state: thread.state, pendingIntentRetained: !!thread.pendingReplyExpansion,
          safeExpandersObserved: thread.expanders, knownReplyIds: [...knownReplyIds(old)]});
        return true;
      };
      const candidates = visible.filter(found => !found.thread.processed && (found.thread.state === 'not_processed' ||
        options.yieldOnWait && found.thread.pendingReplyExpansion) && !deferKnownReplyRestoration(found) &&
        !(replyScope && replyVisits.has(found.thread.rootKey)));
      // An unknown action blocks its own floor, not other known-parent controls.
      const candidate = replyScope ? candidates.find(found => !found.thread.pendingReplyExpansion) ||
        candidates.find(found => replacementDocumentEvidence(found.p, found.thread, found.thread.pendingReplyExpansion)) || candidates[0] : candidates[0];
      if (candidate && processed < options.roots) {
        await expand(candidate);
        continue;
      }
      if (processed >= options.roots || (candidate && replyClicks >= options.maxReplyClicks)) break;
    }
    if (replyScope) {
      replyScopeState = [...threads.values()].some(thread => thread.pendingReplyExpansion)
        ? 'reply_actions_unconfirmed' : 'no_current_safe_controls';
      break; // Known-floor expansion never drives failed parent pagination.
    }
    if (notFound || securityBlock || loginWall || confirmedEmpty() ||
        ((explicitEnd || allExpectedObserved()) && outstandingVisited())) break;
    const before = seenInPass.size;
    // Current XHS layouts can scroll the document instead of the legacy panel.
    const selection = selectRootScroller();
    const actualScroller = selection.element;
    const {candidates: scrollCandidates, selectionBasis} = selection.diagnostic;
    if (rootScrollProgress) rootScrollProgress.lastSelection = selection.diagnostic;
    if (!actualScroller) handoff('comment_scroll_container_unconfirmed');
    checkAccess();
    if (rootScrollProgress) {
      rootScrollProgress.pendingIntent = {state: 'scroll_intent_recorded', attempt: rootScrollProgress.attempts + 1,
        submittedAt: Date.now(), minimumWaitMs: 400, beforeObservedRootIds: [...previousObserved],
        beforeRootKeys: [...seenInPass], selectionBasis, selectedBefore: scrollMetrics(actualScroller), candidates: scrollCandidates};
    }
    actualScroller.scrollTo(0, actualScroller.scrollHeight);
    scrollRounds++;
    if (rootRepairProbe) repairScrolls++;
    if (rootScrollProgress) {
      rootScrollProgress.attempts++;
      rootScrollProgress.pendingIntent.state = 'scroll_submitted';
      rootScrollProgress.pendingIntent.selectedAfter = scrollMetrics(actualScroller);
      rootScrollProgress.state = 'waiting_for_render';
    }
    for (let poll = 0; poll < 15 && withinBudget(); poll++) {
      await wait(400, 'root_render_pending'); scan();
      if (seenInPass.size > before) break;
    }
    if (seenInPass.size > before) stalledScrollRounds = 0;
    else if (++stalledScrollRounds >= 3) break;
  }
  if (withinBudget()) scan();
  else handoff('time_budget_pending');
  } catch (error) {
    if (error.code !== 'DEEP_READ_CHECKPOINT') throw error;
  }
  // The final scope/UID/access check remains mandatory even on a checkpoint.
  checkAccess();
  const allThreads = [...threads.values()];
  const remainingThreads = allThreads.filter(x => !['visible_replies_read', 'previously_read'].includes(x.state));
  const rootPaginationState = replyScope ? 'stalled'
    : rootScrollProgress?.state === 'stalled' ? 'stalled'
    : checkpoint ? 'pending' : confirmedEmpty() || explicitEnd || allExpectedObserved() ? 'complete'
    : stalledScrollRounds >= 3 ? 'stalled' : 'pending';
  const commentTotal = readCommentTotal() ?? initialTotal;
  return { pageUrl: location.href, securityBlock, loginWall, notFound, commentTotal, skippedEmpty,
    results: [...rows.values()], deepCoverage: {
      rootsRequested: options.roots, rootsRendered: allThreads.length, rootsProcessed: processed,
      rootsInDomAtEnd,
      scrollRounds, stalledScrollRounds, replyClicks,
      clickBudgetReached: replyClicks >= options.maxReplyClicks, timeBudgetReached: !withinBudget(),
      elapsedMs: Date.now() - startedAt, remainingThreads, threads: allThreads,
      ...(replyScope ? {discoveredReplyScope: {...replyScope, state: replyScopeState,
        visibleRootIds: allThreads.map(thread => thread.commentId), outsideScopeRootIds: [...outsideReplyScope],
        capturedVisibleRootIds: [...replyVisits.keys()].map(key => key.slice(3)),
        unvisitedRootIds: [...replyRootIds].filter(id => !replyVisits.has('id:' + id)),
        foldedPreviouslyCapturedRootIds: allThreads.filter(thread => replyVisits.has(thread.rootKey) && thread.expanders.length)
          .map(thread => thread.commentId),
        parentPaginationAttempted: false, rootScrollsSubmitted: 0}} : {}),
      ...(discoveryDeferredReplyRoots.size ? {discoveryDeferredReplyRoots: [...discoveryDeferredReplyRoots.values()]} : {}),
      ...(rootScrollProgress ? {rootScrollProgress} : {}),
      ...(checkpoint ? {checkpoint, waitHandedToScheduler: !!options.yieldOnWait} : {}),
      observedRootIds: [...previousObserved], rootPaginationState, explicitEnd,
      documentObservation: {source: 'performance.timeOrigin', timeOrigin: documentTimeOrigin, observedAt: startedAt},
      ...(rootRepairProbe ? {rootRepairObservation: {...rootRepairProbe, scrollsSubmitted: repairScrolls,
        priorRootState: priorRootScroll.state || null, priorStalledRounds: priorRootScroll.stalledRounds || 0}} : {}),
      rootEndBasis: replyScope ? null : confirmedEmpty() ? 'rendered_comment_empty_state' : explicitEnd ? 'rendered_end_signal'
        : allExpectedObserved() ? 'complete_api_root_list' : null,
      commentCountObservation: {adapterInitialTotal: initialTotal, initial: initialCountObservation, latest: latestCountObservation},
      emptyCommentState: emptyCommentState ? {...emptyCommentState, acceptedAsEnd: confirmedEmpty()} : null,
      completeness: 'visible_snapshot_only_not_platform_exhaustion'
    }};
}

export function buildDeepExtractor(builder, { roots = 100, maxReplyClicks = 60, budgetMs = 90000, progress = {}, url: noteUrl = '', accountUid = '', yieldOnWait = false } = {}) {
  if (!Number.isInteger(roots) || roots < 1 || roots > 200 ||
      !Number.isInteger(maxReplyClicks) || maxReplyClicks < 0 || maxReplyClicks > 120 ||
      !Number.isInteger(budgetMs) || budgetMs < 1000 || budgetMs > 150000 || typeof yieldOnWait !== 'boolean') {
    throw new Error('Invalid bounded deep-read budget');
  }
  const code = builder(true, roots);
  const scrollStart = code.indexOf('const driveScroll = () => {');
  const helpersStart = code.indexOf('const clean =', scrollStart);
  const helpersEnd = code.indexOf('const expandReplyThreads = async (root) => {', helpersStart);
  if (scrollStart < 0 || helpersStart < 0 || helpersEnd < 0 || !code.includes('const readCommentTotal =')) {
    throw new Error('Adapter source changed; inspect before updating');
  }
  // Reuse pinned adapter's text/image/like parsing; replace its prefix-only traversal.
  const guard = `
    const expectedScopeUrl = ${JSON.stringify(noteUrl)} || location.href;
    const scopeNoteId = value => value.match(/^https:\\/\\/www\\.xiaohongshu\\.com\\/(?:explore|search_result|user\\/profile\\/[a-f0-9]{24})\\/([a-zA-Z0-9]{16,40})(?:[/?#]|$)/)?.[1];
    if (/website-login\\/(?:captcha|error)|error_code=(?:300017|300031)/.test(location.href)) throw new Error('XHS_ACCESS_RESTRICTED');
    if (!scopeNoteId(expectedScopeUrl) || scopeNoteId(location.href) !== scopeNoteId(expectedScopeUrl)) {
      if (!/^https:\\/\\/www\\.xiaohongshu\\.com\\/(?:chat|explore|search_result|user\\/profile)(?:[/?#]|$)/.test(location.href)) throw new Error('ACCESS_STATE_UNCONFIRMED');
      return {scopeChanged: true, partial: {results: [], coverage: {rootPaginationState: 'pending', scopeChanged: true}}};
    }
  `;
  const prefix = code.slice(0, scrollStart).replace('(async () => {', '(async () => {' + guard);
  return prefix + code.slice(helpersStart, helpersEnd)
    + `try { return await (${readRenderedBatch.toString()})(${JSON.stringify({ roots, maxReplyClicks, budgetMs, progress, noteUrl, accountUid, yieldOnWait })}); }
       catch (error) { if (error.code === 'NOTE_SCOPE_CHANGED') return {scopeChanged: true, partial: error.partial}; throw error; } })()`;
}

export function classifyReadError(message) {
  if (/NOTE_SCOPE_CHANGED/.test(message)) return 'NOTE_SCOPE_CHANGED';
  if (/XHS_ACCESS_RESTRICTED/.test(message)) return 'SECURITY_BLOCK';
  if (/ACCESS_STATE_UNCONFIRMED|Inspected target navigated or closed|Execution context was destroyed|Cannot find context|Target closed/i.test(message)) {
    return 'ACCESS_STATE_UNCONFIRMED';
  }
  return 'DEEP_READ_ERROR';
}

export function validateBoundTarget(config) {
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(config.endpoint || '') ||
      !/^[A-Za-z0-9_-]{1,128}$/.test(config.targetId || '')) throw new Error('ACCESS_STATE_UNCONFIRMED: missing owned target binding');
  const origin = new URL(config.endpoint), ws = new URL(config.targetWebSocketUrl || 'invalid:');
  if (ws.protocol !== 'ws:' || ws.hostname !== '127.0.0.1' || ws.port !== origin.port ||
      ws.username || ws.password || ws.search || ws.hash || ws.pathname !== '/devtools/page/' + config.targetId)
    throw new Error('ACCESS_STATE_UNCONFIRMED: target websocket does not match owned endpoint and ID');
  return ws.href;
}

async function main() {
  const config = JSON.parse(process.argv[2] || '{}');
  if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(config.endpoint || '') ||
      new URL(config.url).hostname !== 'www.xiaohongshu.com') throw new Error('Expected owned endpoint and observed XHS URL');
  const targetWebSocketUrl = validateBoundTarget(config);
  const pkg = path.resolve(config.opencliRoot);
  const metadata = JSON.parse(fs.readFileSync(path.join(pkg, 'package.json'), 'utf8'));
  if (metadata.name !== '@jackwener/opencli') throw new Error('Unexpected adapter package');
  const { CDPBridge } = await import(pathToFileURL(path.join(pkg, 'dist/src/browser/cdp.js')).href);
  const adapter = await import(pathToFileURL(path.join(pkg, 'clis/xiaohongshu/comments.js')).href);
  const expression = buildDeepExtractor(adapter.buildCommentsExtractJs, config);
  const bridge = new CDPBridge();
  let focusHeld = false;
  try {
    const page = await bridge.connect({ cdpEndpoint: targetWebSocketUrl });
    // URL first: a private/non-note surface must never be body-inspected.
    const current = await page.evaluate('({url: location.href, visible: document.visibilityState})');
    if (/website-login\/(?:captcha|error)|error_code=(?:300017|300031)/.test(current.url)) throw new Error('XHS_ACCESS_RESTRICTED');
    const currentUrl = new URL(current.url);
    const publicNoteSurface = current.url === 'about:blank' ||
      (/^https:\/\/www\.xiaohongshu\.com\//.test(current.url) && currentUrl.protocol === 'https:' && currentUrl.hostname === 'www.xiaohongshu.com' &&
        !currentUrl.username && !currentUrl.password && !currentUrl.port && /^\/(?:explore|search_result)(?:\/[a-zA-Z0-9]{16,40})?\/?$/.test(currentUrl.pathname));
    if (!publicNoteSurface) {
      if (currentUrl.hostname === 'www.xiaohongshu.com' && /^\/(?:chat|user\/profile)(?:[/?#]|$)/.test(currentUrl.pathname)) throw new Error('NOTE_SCOPE_CHANGED');
      throw new Error('ACCESS_STATE_UNCONFIRMED');
    }
    {
      await bridge.send('Page.bringToFront');
      // Normal DevTools focus control prevents background timer throttling and
      // suspended IntersectionObserver loading in this owned temporary page.
      await bridge.send('Emulation.setFocusEmulationEnabled', {enabled: true});
      focusHeld = true;
      if (await page.evaluate('document.hidden')) throw new Error('BROWSER_BACKGROUND: restore only the owned temporary window before continuing');
    }
    const sameNote = new URL(current.url).hostname === 'www.xiaohongshu.com' &&
      new URL(current.url).pathname === new URL(config.url).pathname;
    if (!sameNote) {
      await page.goto(config.url);
      await page.wait({ time: 3 });
    }
    // No navigation retries, including when a restriction appears.
    const evaluated = await bridge.send('Runtime.evaluate', {
      expression, returnByValue: true, awaitPromise: true,
    }, (config.budgetMs || 90000) + 20000);
    if (evaluated.exceptionDetails) throw new Error(evaluated.exceptionDetails.exception?.description || 'Evaluation failed');
    const data = evaluated.result?.value;
    if (data?.scopeChanged) {
      const partialRows = adapter.normalizeCommentRows(data.partial?.results || []);
      const partial = {data: partialRows.map((row, index) => ({...data.partial.results[index], ...row, rank: index + 1,
        userId: adapter.parseXhsProfileHref(row.authorHrefRaw), profileUrl: adapter.buildXhsProfileUrl(row.authorHrefRaw)})),
        coverage: data.partial?.coverage || {rootPaginationState: 'pending', scopeChanged: true}};
      throw Object.assign(new Error('NOTE_SCOPE_CHANGED'), {partial});
    }
    if (!data || data.securityBlock || data.loginWall) throw new Error('XHS_ACCESS_RESTRICTED');
    const rows = adapter.normalizeCommentRows(data.results);
    adapter.assertCommentsLoaded(data, rows.length, 'deep-comments');
    const normalized = rows.map((row, index) => ({ ...data.results[index], rank: index + 1, ...row,
      userId: adapter.parseXhsProfileHref(row.authorHrefRaw),
      profileUrl: adapter.buildXhsProfileUrl(row.authorHrefRaw),
    }));
    process.stdout.write(JSON.stringify({ ok: true, data: normalized, coverage: {
      ...data.deepCoverage, platformCommentTotal: data.commentTotal, skippedEmpty: data.skippedEmpty,
      adapterVersion: metadata.version,
    }}));
  } finally {
    if (focusHeld) await bridge.send('Emulation.setFocusEmulationEnabled', {enabled: false}).catch(() => {});
    await bridge.close();
  }
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  main().catch(error => {
    const message = String(error?.message || error);
    process.stdout.write(JSON.stringify({ ok: false, error: {
      code: classifyReadError(message), message: message.slice(0, 250),
    }, ...(error?.partial ? {partial: error.partial} : {})}));
    process.exitCode = 1;
  });
}
