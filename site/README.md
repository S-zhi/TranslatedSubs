# TranslatedSubs 产品宣传页

当前已实现产品首屏、宣传视频、核心优势、开始使用双栏入口与多列页脚。固定的共享背景随滚动从 GhostFibers 淡化到 Particles，粒子连续延伸到第三屏；开始使用区域平滑收束到页脚底色。视频区使用明确标注的占位视频；标题使用 SplitText 逐字动画。React + Vite 页面独立于 `web/` 中的业务工作台，不需要启动 Python API、Redis 或媒体处理服务。

## 本地开发

需要 Node.js 20.19+（20.x）或 22.12+，以及 npm。从仓库根目录运行：

```bash
npm ci --prefix site
```

```bash
npm --prefix site run dev
```

打开 http://127.0.0.1:5173/。端口被占用时会直接报错，避免误开另一个服务。

Claude Desktop 中可使用本地 `.claude/launch.json` 的 `product-hero` 配置启动预览；该文件被仓库忽略。配置命令与上面相同，端口为 5173。

## 构建与预览

```bash
npm --prefix site run build
```

```bash
npm --prefix site run preview
```

预览地址是 http://127.0.0.1:4173/。`site/dist/` 可作为独立静态站点发布，构建资源采用相对路径。Vite preview 仅用于本地验收。当前 FastAPI 不托管该目录，宣传页也不请求业务 API。

## 内容和交互

- `src/App.jsx`：首屏文案、导航、使用指南和 GitHub 链接、主题与动效控制。
- `src/styles.css`：首屏布局、主题变量、响应式样式和文字入场效果。
- `src/components/GhostFibers.jsx` / `.css`：用户指定的 React Bits 光纤组件。
- `src/components/VideoSection.jsx` / `.css`：第二屏标题、占位播放器、播放状态、滚动离屏暂停与加载失败重试。
- `src/components/Particles.jsx` / `.css`：第二、第三屏共用的粒子背景，按独立模块延迟加载。
- `src/components/BackgroundStage.jsx` / `.css`：固定背景与滚动叠化，仅在光纤、混合、粒子模式边界切换 React 状态；逐帧透明度由 GSAP 控制。
- `src/components/SplitText.jsx`：用户指定的 React Bits 逐字标题，使用 GSAP / `@gsap/react`。
- `src/components/AdvantagesSection.jsx` / `.css`：第四版文字为主的核心优势区，分别说明可插拔插件、本地与云端模型、多平台来源、处理流程扩展。
- `src/components/TrueFocus.jsx` / `.css`：参考 React Bits True Focus 的关键词聚焦框，适配现有 GSAP、悬停、视口变化及全页暂停状态。
- `src/components/GetStartedSection.jsx` / `.css`：自行部署与联系沟通两张卡片。
- `src/components/SiteFooter.jsx` / `.css`：品牌、多列导航、GitHub 和返回顶部。
- `src/content/site-links.js`：卡片与页脚使用的真实仓库及文档链接，集中维护。
- `public/media/`：本地占位视频、封面与 [素材来源说明](public/media/README.md)。
- `public/brand.svg`：复用仓库 `docs/assets/eye-subtitles-logo.svg`。

主题跟随系统设置，可手动切换。背景支持鼠标聚焦和点击光脉冲；暂停时关闭背景交互。系统开启“减少动态效果”时保持静态画面。手机屏幕减少粒子数量和渲染分辨率。无 WebGL2 时仍显示文案、按钮与静态底色。

“开始使用”和“使用文档”连接现有本地快速启动指南。“宣传视频”与“核心优势”导航分别跳到 `#film`、`#advantages`，也可以自然向下滚动到达。视频与优势区不强制满屏，按内容自然撑高以减少段间空白。视频不自动播放，点击后开启原生控制，播放时暂停共享背景；视频离屏或标签页隐藏时暂停播放，结束后可重播。背景暂停状态由三屏共享。

正式宣传片尚未制作，目前使用 5 秒 MDN CC0 花朵视频占位。替换时修改 `VideoSection.jsx` 顶部的 `VIDEO` 源和封面，同步更新可访问名称、字幕与占位说明。开始使用区提供 GitHub 自行部署与现有公开联系渠道；页脚只列出真实存在的文档和社区入口。没有虚构销售承诺、联系方式、社交账号或条款页面。FAQ 留待后续制作。

## 验证

常规文案和版式迭代采用最小验证：运行一次 `npm --prefix site run build`，检查桌面/手机无横向溢出，再确认本次新增或修改的交互及控制台错误。只有改变播放器、共享背景或主题逻辑时，才扩大到相关行为检查；不默认运行后端或全量测试。生产构建可独立打开，不依赖 Vite 开发端点。

## 第三方组件

GhostFibers 来自 [React Bits](https://github.com/DavidHDev/react-bits)，固定版本 [14330e6](https://github.com/DavidHDev/react-bits/commit/14330e6963f6c5ddd773ee2af844d0630c22b958)，与本次用户提供的 JavaScript + CSS 实现对应。上游源码保留，参数在 `BackgroundStage.jsx` 中调整。

Particles 固定版本 [482ab1c](https://github.com/DavidHDev/react-bits/commit/482ab1cd3f567467a8dc9f49683bcefe54223e2c)，采用用户指定的 JavaScript + CSS WebGL2 实现，无额外运行依赖。集成时保留所有 GL 资源和监听器清理，但移除了 cleanup 中的主动 `loseContext()`，以兼容 React StrictMode 重复初始化同一 canvas。

SplitText 与 GhostFibers 固定到同一上游提交 `14330e6`，组件使用官方 GSAP SplitText 与 ScrollTrigger；中文逐字进入后保持显示，不循环隐藏，系统减少动态效果时直接显示完整文本。

True Focus 的视觉参考固定提交 `14330e6` 的 [上游组件](https://github.com/DavidHDev/react-bits/blob/14330e6963f6c5ddd773ee2af844d0630c22b958/src/content/TextAnimations/TrueFocus/TrueFocus.jsx)。本站用现有 GSAP 实现相同四角框与关键词模糊效果，避免引入 Motion；仅可见时自动轮换，暂停时停止，减少动态效果时文字全部清晰且不轮换。

React Bits 组件及视觉适配遵循 MIT + Commons Clause，原文保存在 [public/licenses/react-bits.md](public/licenses/react-bits.md)，构建时随站点分发。GSAP 与 `@gsap/react` 遵循 [GreenSock standard no-charge license](https://gsap.com/standard-license)，不视为 MIT。仓库其他代码的许可证不因此改变。Manrope 字体通过 npm 自托管，浏览器无需连接外部字体 CDN。
