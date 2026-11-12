// Prepare only the owned temporary CDP browser. Never connect to the original profile.
import fs from 'node:fs';
import { pathToFileURL } from 'node:url';
import { validateBoundTarget } from './deep_comments.mjs';

const [bridgePath, endpoint, cookiePath, targetWebSocketUrl, targetId] = process.argv.slice(2);
if (!bridgePath || !/^http:\/\/127\.0\.0\.1:\d+$/.test(endpoint || '') || !cookiePath) {
  throw new Error('Expected bridge module, owned loopback endpoint and private cookie file');
}
validateBoundTarget({endpoint, targetWebSocketUrl, targetId});
const { CDPBridge } = await import(pathToFileURL(bridgePath).href);
const bridge = new CDPBridge();
try {
  const page = await bridge.connect({ cdpEndpoint: targetWebSocketUrl });
  const current = await page.evaluate('location.href');
  if (/website-login\/(?:captcha|error)|error_code=(?:300017|300031)/.test(current)) throw new Error('XHS_ACCESS_RESTRICTED');
  const currentUrl = new URL(current);
  if (current !== 'about:blank' && !(/^https:\/\/www\.xiaohongshu\.com\//.test(current) && currentUrl.protocol === 'https:' && currentUrl.hostname === 'www.xiaohongshu.com' &&
      !currentUrl.username && !currentUrl.password && !currentUrl.port && /^\/(?:explore|search_result)(?:\/[a-zA-Z0-9]{16,40})?\/?$/.test(currentUrl.pathname)))
    throw new Error('NOTE_SCOPE_CHANGED: bootstrap target is not a public research surface');
  const session = JSON.parse(fs.readFileSync(cookiePath, 'utf8'));
  const cookies = Object.entries(session)
    .filter(([name, value]) => name !== 'saved_at' && typeof value === 'string')
    .map(([name, value]) => ({ name, value, domain: '.xiaohongshu.com', path: '/', secure: true }));
  if (!cookies.some(c => c.name === 'web_session')) throw new Error('Missing scoped session');
  await bridge.send('Network.setCookies', { cookies });
  await page.goto('https://www.xiaohongshu.com/explore');
  process.stdout.write(JSON.stringify({ temporary_browser_prepared: true }));
} finally {
  await bridge.close();
}
