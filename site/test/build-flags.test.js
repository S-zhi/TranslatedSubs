import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

// 这些断言守住“关闭构建真的干净”这条不变量。build-flags.js 只能在 Vite 里
// 导入（它导出的是被 define 替换的裸标识符），所以这里读源码文本而不是
// import 模块 —— 与 web/__tests__/trial-token.test.mjs 的做法一致。
const read = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');

const app = read('../src/App.jsx');
const getStarted = read('../src/components/GetStartedSection.jsx');
const siteLinks = read('../src/content/site-links.js');
const buildFlags = read('../src/lib/build-flags.js');

test('build-flags exports the define identifier directly so Rollup can fold it', () => {
  // 任何 typeof 守卫 / 三元包装都会让 Rollup 看到普通变量，死代码消除失效。
  assert.match(buildFlags, /export const CLOUD_TRIAL_ENABLED = __CLOUD_TRIAL_ENABLED__;/);
  assert.doesNotMatch(buildFlags, /typeof\s+__CLOUD_TRIAL_ENABLED__/);
});

test('every trial entry point in App.jsx is gated by CLOUD_TRIAL_ENABLED', () => {
  assert.match(app, /import \{ CLOUD_TRIAL_ENABLED \} from '\.\/lib\/build-flags\.js'/);

  // 首屏主操作：开启时渲染试用组件，关闭时退回“开始使用”指向本地指南。
  assert.match(app, /\{CLOUD_TRIAL_ENABLED \? \(\s*<TrialAction reducedMotion=\{reducedMotion\} \/>/);
  assert.match(app, /<a className="button button--primary" href=\{GUIDE\}[^>]*>\s*开始使用/);

  // 首屏“查看试用状态”链接只能出现在开关为真的分支里。
  const guideIndex = app.indexOf('hero__cloud-guide');
  assert.ok(guideIndex > 0, 'hero__cloud-guide should still exist for the ON build');
  assert.ok(
    app.lastIndexOf('CLOUD_TRIAL_ENABLED', guideIndex) > app.lastIndexOf('</div>', guideIndex) - 1,
    'hero__cloud-guide must sit behind CLOUD_TRIAL_ENABLED',
  );

  // 开始使用区：开启时用带试用的包装组件，关闭时用纯部署版本。
  assert.match(app, /\{CLOUD_TRIAL_ENABLED \? <GetStartedSectionWithTrial \/> : <GetStartedSection \/>\}/);

  // 关闭构建不得引入任何无条件触发的试用副作用。
  assert.doesNotMatch(app, /^\s*useCloudTrial\(/m);
  assert.doesNotMatch(app, /^\s*const trial = /m);
});

test('the hero and trial panel share one controller for the whole page', () => {
  const slot = read('../src/components/CloudTrialSlot.jsx');
  assert.match(app, /const TrialScope = CLOUD_TRIAL_ENABLED \? CloudTrialProvider : Fragment;/);
  assert.match(app, /return <TrialScope>\{content\}<\/TrialScope>;/);
  assert.equal((slot.match(/useCloudTrial\(\)/g) || []).length, 1);
  assert.equal((slot.match(/useContext\(CloudTrialContext\)/g) || []).length, 2);
});

test('GetStartedSection only renders the trial panel and copy behind the flag', () => {
  assert.match(getStarted, /import \{ CLOUD_TRIAL_ENABLED \} from '\.\.\/lib\/build-flags\.js'/);
  assert.match(getStarted, /\{CLOUD_TRIAL_ENABLED && <CloudQuickStart /);
  assert.match(
    getStarted,
    /\{CLOUD_TRIAL_ENABLED \? '先在云端体验，再选择自己的部署方式。' : '自行部署，或和我们聊聊你的使用场景。'\}/,
  );
  // 试用面板必须是静态 import：动态 import() 会在摇树前建立 chunk，
  // 关闭构建仍会留下孤儿产物。
  assert.match(getStarted, /^import CloudQuickStart from '\.\/CloudQuickStart\.jsx';$/m);
  assert.doesNotMatch(getStarted, /import\(/);
});

test('the footer drops the #cloud-start link when the trial is off', () => {
  assert.match(siteLinks, /import \{ CLOUD_TRIAL_ENABLED \} from '\.\.\/lib\/build-flags\.js'/);
  assert.match(siteLinks, /const cloudTrialLinks = CLOUD_TRIAL_ENABLED\s*\?\s*\[\{ label: '云端试用', href: '#cloud-start' \}\]\s*:\s*\[\];/);
  assert.match(siteLinks, /\.\.\.cloudTrialLinks/);
});

test('the build scripts make both states explicit and discoverable', () => {
  const pkg = JSON.parse(read('../package.json'));
  assert.equal(pkg.scripts.build, 'vite build');
  assert.equal(pkg.scripts['build:no-trial'], 'SUBTRANS_ENABLE_CLOUD_TRIAL=0 vite build');
});

test('vite.config defaults the trial on and only the literal 0 turns it off', () => {
  const config = read('../vite.config.js');
  assert.match(config, /process\.env\.SUBTRANS_ENABLE_CLOUD_TRIAL !== '0'/);
  assert.match(config, /define: \{ __CLOUD_TRIAL_ENABLED__: cloudTrialEnabled \}/);
});
