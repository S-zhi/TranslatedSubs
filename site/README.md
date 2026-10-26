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

预览地址是 http://127.0.0.1:4173/。`site/dist/` 可作为独立静态站点发布，构建资源采用相对路径。Vite preview 仅用于本地验收。当前业务 FastAPI 不托管该目录。

## 云端试用开关

云端试用是**官网部署专属**能力，由构建期开关控制，改后必须重新构建：

| 构建命令 | 结果 |
| --- | --- |
| `npm --prefix site run build`（默认） | 官网构建：试用入口与行为同本文描述 |
| `npm --prefix site run build:no-trial` | 纯宣传页：无试用面板、无 `#cloud-start`、页脚无「云端试用」；首屏主按钮为「开始使用」并跳转使用文档；不发起任何 `/api/trials` 请求 |

开关是 `SUBTRANS_ENABLE_CLOUD_TRIAL`（非 `VITE_` 前缀，属于构建者而非浏览器配置）：默认开启，只有字面量 `0` 表示关闭。关闭时 Vite 的 `define` 把开关内联成字面量 `false`，Rollup 随之摇掉试用模块，构建产物中不含试用代码。等价写法是 `SUBTRANS_ENABLE_CLOUD_TRIAL=0 npm --prefix site run build`。

关闭构建仍可能保留未使用的试用 CSS 选择器，但不会挂载试用组件或加载试用 API 客户端。

开关值会随打包进入浏览器，它只表示「此部署提供试用」，不含任何密钥；InstaBox API Key 与 `SUBTRANS_LAUNCHER_SECRET` 只存在于服务端控制服务。关闭构建下试用功能**完全不存在**，而不是显示不可用；只有开启构建但控制服务未配置时，才会显示「云端试用暂未开放」这类真实状态。控制服务部署见 [InstaBox 一键试用部署文档](../docs/instabox-quick-start.md)；`install.sh --with-cloud-trial` 可额外安装控制服务，安装器不收集任何密钥。

## 内容和交互

- `src/App.jsx`：首屏文案、平台标识与处理流程信息条、导航、使用指南和 GitHub 链接、主题与视频播放状态。
- `src/styles.css`：首屏布局、主题变量、响应式样式和文字入场效果。
- `src/components/HeroPreview.jsx` / `.css`：首屏右侧双语字幕动画示意，使用现有 GSAP 循环展示四组原文和译文，波形起伏、时间轴播放头、片段填充与处理步骤同步变化。离屏、标签页隐藏和宣传视频播放时暂停；系统减少动态效果时显示静态首句。手机端堆叠到文案下方。
- `src/components/GhostFibers.jsx` / `.css`：用户指定的 React Bits 光纤组件。
- `src/components/VideoSection.jsx` / `.css`：第二屏标题、占位播放器、播放状态、滚动离屏暂停与加载失败重试。
- `src/components/Particles.jsx` / `.css`：第二、第三屏共用的粒子背景，按独立模块延迟加载。
- `src/components/BackgroundStage.jsx` / `.css`：固定背景与滚动叠化，仅在光纤、混合、粒子模式边界切换 React 状态；逐帧透明度由 GSAP 控制。
- `src/components/SplitText.jsx`：用户指定的 React Bits 逐字标题，使用 GSAP / `@gsap/react`。
- `src/components/AdvantagesSection.jsx` / `.css`：第四版文字为主的核心优势区，分别说明可插拔插件、本地与云端模型、多平台来源、处理流程扩展。
- `src/components/TrueFocus.jsx` / `.css`：参考 React Bits True Focus 的关键词聚焦框，适配现有 GSAP、悬停、视口变化及系统减少动态效果设置。
- `src/components/GetStartedSection.jsx` / `.css`：云端快速使用、自行部署与联系沟通入口。
- `src/components/CloudQuickStart.jsx` / `.css`：共享的云端试用创建、真实启动阶段、就绪入口、到期与结束试用交互。
- `src/hooks/use-cloud-trial.js`、`src/lib/cloud-trial-{api,controller}.js`：同源 API、状态恢复、重复点击去重、GET-only 异常恢复与轮询清理。私有 VM 启动脚本位于 `../src/instabox_launcher/bootstrap.sh`，不再打包进网页。
- `src/components/SiteFooter.jsx` / `.css`：品牌、多列导航、GitHub 和返回顶部。
- `src/content/site-links.js`：卡片与页脚使用的真实仓库及文档链接，集中维护。
- `public/media/`：本地占位视频、封面与 [素材来源说明](public/media/README.md)。
- `public/brand.svg`：复用仓库 `docs/assets/eye-subtitles-logo.svg`。

主题跟随系统设置，可手动切换。首屏标题上方标注“TranslatedSubs视频字幕工作台”，底部信息条展示 YouTube、TikTok、X / Twitter、本地视频标识，以及导入、转写、翻译、校对、导出的处理流程。背景支持鼠标聚焦和点击光脉冲；视频播放时自动暂停并关闭背景交互。系统开启“减少动态效果”时保持静态画面。手机屏幕减少粒子数量和渲染分辨率。无 WebGL2 时仍显示文案、按钮与静态底色。

**仅在开启试用开关的构建中**，首屏“快速使用”与 `#cloud-start` 共用试用状态：点击后通过维护方部署的服务端启动器，在维护方 InstaBox 账号内创建独立机器；页面只查询进度并在就绪后提供自动登录的工作台链接。访客无需 InstaBox 账号或复制脚本。刷新恢复已有试用，重复点击不会重复创建；缺配置时明确显示在线体验未开放。InstaBox API Key 仅保存在启动器，不进入前端环境变量或试用机。“使用文档”仍连接现有本地指南；服务端配置、同源转发及基础服务与模型的边界见 [InstaBox 一键试用部署文档](../docs/instabox-quick-start.md)。“宣传视频”与“核心优势”导航分别跳到 `#film`、`#advantages`，也可以自然向下滚动到达。视频与优势区不强制满屏，按内容自然撑高以减少段间空白。视频不自动播放，点击后开启原生控制，播放时暂停共享背景；视频离屏或标签页隐藏时暂停播放，结束后可重播。页面不再提供单独的手动动效暂停按钮。

正式宣传片尚未制作，目前使用 5 秒 MDN CC0 花朵视频占位。替换时修改 `VideoSection.jsx` 顶部的 `VIDEO` 源和封面，同步更新可访问名称、字幕与占位说明。开始使用区提供 GitHub 自行部署与现有公开联系渠道；页脚只列出真实存在的文档和社区入口。没有虚构销售承诺、联系方式、社交账号或条款页面。FAQ 留待后续制作。

## 验证

常规文案和版式迭代采用最小验证：运行一次 `npm --prefix site run build`，检查桌面/手机无横向溢出，再确认本次新增或修改的交互及控制台错误。云端试用状态与客户端测试使用 `npm --prefix site test`；控制服务和工作台鉴权的相关测试见 [InstaBox 文档](../docs/instabox-quick-start.md#验证与上线验收)。只扩大到本次受影响的行为，不默认运行全量测试。`build:no-trial` 构建应检查首屏主按钮为「开始使用」、无 `#cloud-start` 与页脚「云端试用」，且网络面板零条 `/api/trials` 请求；只有开启试用的构建在未配置同源控制服务时才显示试用暂不可用。

## 第三方组件

GhostFibers 来自 [React Bits](https://github.com/DavidHDev/react-bits)，固定版本 [14330e6](https://github.com/DavidHDev/react-bits/commit/14330e6963f6c5ddd773ee2af844d0630c22b958)，与本次用户提供的 JavaScript + CSS 实现对应。上游源码保留，参数在 `BackgroundStage.jsx` 中调整。

Particles 固定版本 [482ab1c](https://github.com/DavidHDev/react-bits/commit/482ab1cd3f567467a8dc9f49683bcefe54223e2c)，采用用户指定的 JavaScript + CSS WebGL2 实现，无额外运行依赖。集成时保留所有 GL 资源和监听器清理，但移除了 cleanup 中的主动 `loseContext()`，以兼容 React StrictMode 重复初始化同一 canvas。

SplitText 与 GhostFibers 固定到同一上游提交 `14330e6`，组件使用官方 GSAP SplitText 与 ScrollTrigger；中文逐字进入后保持显示，不循环隐藏，系统减少动态效果时直接显示完整文本。

True Focus 的视觉参考固定提交 `14330e6` 的 [上游组件](https://github.com/DavidHDev/react-bits/blob/14330e6963f6c5ddd773ee2af844d0630c22b958/src/content/TextAnimations/TrueFocus/TrueFocus.jsx)。本站用现有 GSAP 实现相同四角框与关键词模糊效果，避免引入 Motion；仅可见时自动轮换，暂停时停止，减少动态效果时文字全部清晰且不轮换。

React Bits 组件及视觉适配遵循 MIT + Commons Clause，原文保存在 [public/licenses/react-bits.md](public/licenses/react-bits.md)，构建时随站点分发。GSAP 与 `@gsap/react` 遵循 [GreenSock standard no-charge license](https://gsap.com/standard-license)，不视为 MIT。仓库其他代码的许可证不因此改变。Manrope 字体通过 npm 自托管，浏览器无需连接外部字体 CDN。
