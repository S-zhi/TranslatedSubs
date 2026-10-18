# macOS 桌面发行检查表

状态：开发中，2026-10-17。**没有可下载的已签名、公证桌面正式版。** 本文是发行操作约定和待验收清单，不是已完成记录，也不授权购买、上传发行资产或公开发布。产品门槛见[产品约定](./macos-desktop-v1-product-brief.md)，工程接口见[实施契约](./macos-desktop-v1-implementation.md)，开发入口见[安装与开发指南](./macos-desktop-install.md)。

## 发行门槛与证据

每个候选包记录源码提交、工作树是否有未提交改动、锁文件、构建工具版本、应用版本、构建机器／macOS、DMG 大小和 SHA-256、模型 revision、测试日期与执行人。以下状态在获得证据前均为 **未验证**；不得因为脚本存在、开发机可启动或 CI 单测通过就改成通过。

| 门槛 | 必须留下的证据 | 当前状态 |
| --- | --- | --- |
| 干净安装 | 无 Python、Redis、Homebrew 等开发依赖的 Apple Silicon Mac，从实际下载的 DMG 安装、首启、准备模型、完成预览和导出 | 未验证 |
| 系统兼容 | 每个声明支持的 macOS 版本、架构及依赖加载记录；13.0 仅是暂定部署目标 | 未验证，最低支持版本未确定 |
| 性能 | M1／8GB、60 分钟 1080p 英文课程；模型已就绪、视频已在本地；转写＋翻译＋硬字幕压制总耗时不超过两小时，记录分段耗时、资源占用和成品 | 未验证 |
| 字幕质量 | 同一样本全长无整段漏字幕、持续明显错位或大段漏译；开头／中间／结尾抽查能表达主要意思，保留问题和时间点；标明机器生成 | 未验证 |
| 生命周期 | 关闭窗口继续任务、菜单栏重开；主动退出有任务时确认；重开保留历史与完成结果，中断任务可重试；不承诺断点继续 | 未验证 |
| 更新 | 新版本与说明、有效 DMG 下载入口；失败提示；未配置渠道不冒充最新；不会自动安装、重启或中断当前任务 | 未验证 |
| 签名与公证 | 完整包签名校验、Apple Accepted 记录、stapler 验证、真实下载后的 Gatekeeper 安装记录 | 未验证 |

验收脚本为 [`scripts/desktop/benchmark.py`](../scripts/desktop/benchmark.py)，执行说明和证据要求见[桌面验收说明](./desktop-acceptance.md)。使用真实且有权处理的课程样本；单测、短片和 16GB 开发机结果不能替代目标机长视频验收。首次模型下载耗时、YouTube 下载耗时单独记录。

国内、海外必须独立填写下表，记录地区、网络类型、日期、候选包哈希、成功／失败与耗时；模型缓存须清空或使用明确隔离的验收目录来验证首次下载，不使用缓存命中结果冒充网络验收。

| 项目 | 国内 | 海外 | 通过条件 |
| --- | --- | --- | --- |
| DMG 下载与安装 | 未验证 | 未验证 | 从实际发行入口下载完整文件，哈希、签名、公证一致 |
| 首次模型准备 | 未验证 | 未验证 | 下载、转换、校验均完成；故障可重试，大小未知时不伪造进度 |
| 更新检查与 DMG 下载 | 未验证 | 未验证 | 真实 HTTPS manifest 与下载入口可用，不只有首页返回 200 |
| 本地视频闭环 | 未验证 | 未验证 | 模型就绪后本地转写、翻译、压制、预览、导出，不依赖云端推理 |
| YouTube 公开视频 | 未验证 | 未验证 | 网络可访问目标视频时实际下载成功；不可达时明确原因并可转本地视频 |

应用不改变 YouTube 的地区或网络访问条件；网页可打开也不等于媒体流下载成功。任一必验项失败就继续修正并顺延发布，不自动改发未签名预览版。

## 构建、签名、公证与 Gatekeeper

交付任务预期 `scripts/desktop/build.sh`、`scripts/desktop/sign-and-notarize.sh`；当前实际文件是 [`scripts/desktop/build.py`](../scripts/desktop/build.py)，两个 `.sh` 文件尚不存在。`--mode dev` 构建开发包，`--mode release` 包含签名公证；此文件名差异应由集成负责人确认或补齐入口，不能声称已经验证不存在的脚本。构建阶段生成完整 Apple Silicon `.app` 和候选 DMG，并检查包内后端、FFmpeg/FFprobe、动态库、字体与 Web 资源；版本号须贯穿应用、后端和发行文件。

正式签名前由发行负责人准备以下条件，不在仓库或日志中保存任何私钥、密码、API key 或配置导出：

- 有效 Apple Developer Program 会员及相应团队权限；钥匙串中存在有效 **Developer ID Application** 证书及配对私钥，并允许签名工具使用。只有证书文件、Apple Development 或 ad-hoc 身份都不满足此次直接分发要求。
- 能使用 `codesign`、`xcrun notarytool`、`xcrun stapler` 的 Xcode 工具链。`notarytool` 可执行不代表有公证授权。
- 由负责人通过安全方式配置的公证认证：Apple Account 的 app-specific password 与 Team ID，或受支持的 App Store Connect API key；脚本使用已有钥匙串 profile，不把凭据放进命令示例或 CI 输出。自动化钥匙串访问也必须显式配置，不能把解锁个人钥匙串当作默认步骤。
- 对主应用、Python 后端、扩展模块、FFmpeg 及所有嵌套 Mach-O 从内向外签名，使用 Hardened Runtime 和安全时间戳；审查实际所需 entitlement，不携带调试 `get-task-allow`。当前 `desktop/packaging/entitlements.plist` 仍需以真实依赖运行结果审核。

这些是 [Apple 公证要求](https://developer.apple.com/documentation/security/notarizing-macos-software-before-distribution)和[自定义公证流程](https://developer.apple.com/documentation/security/customizing-the-notarization-workflow)的执行前提。PyInstaller 会默认进行 ad-hoc 签名；真实 Developer ID 的使用方式及 Hardened Runtime 说明见 [PyInstaller 官方文档](https://pyinstaller.org/en/stable/feature-notes.html#macos-binary-code-signing)，本项目必须检查最终完整包，不能只检查 Swift 主程序。

发行顺序：完成依赖清单与签名 → 提交公证并取得 `Accepted` → 给 `.app`／DMG 装订公证票据并验证 → 对最终文件重新计算 SHA-256 → 从真实下载入口进行 Gatekeeper 验证。当前 `build.py --mode release` 先公证 `.app` 的 ZIP，再装订 `.app`、生成 DMG，随后签名、公证、装订 DMG；任何内容修改都要重新执行相应签名、公证和校验。`notarytool` 提交需要联网且上传构建产物，应由已获授权的发行负责人运行。

完成[开发依赖与资产准备](./macos-desktop-install.md#开发者从本工作树构建)后，负责人设置实际路径、签名身份名称、已有钥匙串 profile、真实 HTTPS 渠道地址和更新说明文件，再执行下例。这里的身份名称／profile 不是私钥或密码，不要在变量中填入凭据内容：

```sh
.venv/bin/python scripts/desktop/build.py --mode release \
  --ffmpeg-dir "$DESKTOP_FFMPEG_DIR" --ffmpeg-archive "$DESKTOP_FFMPEG_ARCHIVE" \
  --font-dir "$DESKTOP_FONT_DIR" --font-archive "$DESKTOP_FONT_ARCHIVE" \
  --identity "$DESKTOP_SIGNING_IDENTITY" --notary-profile "$DESKTOP_NOTARY_PROFILE" \
  --update-url "$DESKTOP_UPDATE_URL" --download-url "$DESKTOP_DOWNLOAD_URL" \
  --release-notes "$DESKTOP_RELEASE_NOTES_FILE" --output "$DESKTOP_OUTPUT_DIR"
```

输出为 `TranslateSub.app`、`TranslateSub-<version>-macos-arm64-release.dmg`、对应 `.sha256` 和 `update-manifest.json`；脚本不会发布这些文件。包内 `source-inventory.json`、`dependency-audit.json`、`python-dependencies.lock` 为审核输入，环境包列表不等于完整、准确的冻结产物 SBOM。`scripts/desktop/ci-build.sh` 是 CI 专用入口，会配置临时签名钥匙串及其搜索列表，不作为普通开发机本地启动命令。

以下只读验证由负责人将变量设为**实际候选产物绝对路径**后执行，不是绕过系统保护的安装教程：

```sh
codesign --verify --deep --strict --verbose=2 "$DESKTOP_APP_PATH"
codesign --display --verbose=4 "$DESKTOP_APP_PATH"
xcrun stapler validate "$DESKTOP_APP_PATH"
xcrun stapler validate "$DESKTOP_DMG_PATH"
spctl --assess --type execute --verbose=4 "$DESKTOP_APP_PATH"
spctl --assess --type open --context context:primary-signature --verbose=4 "$DESKTOP_DMG_PATH"
shasum -a 256 "$DESKTOP_DMG_PATH"
```

保存已脱敏的签名类型、团队、验证结果、公证 submission ID 和状态。若流程只给 DMG 装订，不能声称 `.app` 的 `stapler validate` 也已通过；需记录实际流程并验证复制安装后的启动体验。随后在 Gatekeeper 默认开启的干净 Mac 上，用浏览器下载最终 DMG、保留隔离属性、复制到“应用程序”并首次启动，检查在线与断网启动体验。不要用开发机无隔离属性的本地产物代替这一检查，也不要删除隔离属性或禁用 Gatekeeper。Apple 要求检验真实用户启动体验，见[直接分发说明](https://help.apple.com/xcode/mac/current/en.lproj/dev033e997ca.html)。

## 依赖、模型与许可证清单

下面是发行审核范围，不是已生成的完整 SBOM。每个实际随包文件和首启下载模型都要记录准确版本／revision、来源、哈希、许可证正文、版权声明和必要源码／构建信息。未核实项标为 **UNKNOWN**，解决前不能宣称再分发审核完成。仓库 MIT 许可证不能覆盖第三方组件的义务。

| 项目 | 当前来源／许可依据 | 发行前要补齐的记录 |
| --- | --- | --- |
| 项目源码、原生壳、Web 资源 | 仓库 [LICENSE](../LICENSE)；第三方资源另列 | 提交号、版权及修改记录；逐项排查打入包的资源 |
| Python、Swift 运行依赖及系统框架 | 实际打包版本和许可证清单 **UNKNOWN** | Python 解释器及动态库来源、版本、许可；识别系统提供与随包分发的边界 |
| PyInstaller／bootloader | [官方许可](https://pyinstaller.org/en/stable/license.html)：GPL 及打包例外，部分文件 Apache 2.0 | 以锁定版本完整许可为准，保留相应文本；不能由例外推断所有依赖均可任意分发 |
| faster-whisper、CTranslate2、Torch、Transformers、tokenizers、SentencePiece、sacremoses、Hugging Face Hub、yt-dlp、FastAPI／uvicorn 及其传递依赖 | 版本依据 `uv.lock`；实际冻结包清单及逐项许可 **UNKNOWN** | 收集发行包内实际 distributions、原生扩展及独立二进制的 LICENSE／NOTICE，不能只看直接依赖 |
| FFmpeg／FFprobe | [官方许可说明](https://ffmpeg.org/legal.html)：通常 LGPL 2.1+，启用部分组件会变为 GPL；最终二进制来源 **UNKNOWN** | 记录 `-version`、`-buildconf`、`-L`、源码及构建方法、动态库依赖、编解码器选项；遇到 nonfree 配置必须解决分发限制，不能直接复制开发机二进制发布 |
| libass 与字体渲染链 | [libass 上游 COPYING](https://github.com/libass/libass/blob/master/COPYING) 为 ISC；FreeType／HarfBuzz／FriBidi 等实际依赖和许可 **UNKNOWN** | 完整动态库闭包、各自声明；确认 `subtitles` 滤镜和中文渲染在干净设备可用 |
| 字体、图标及其他前端静态资源 | 最终选用字体、Phosphor 等实际文件来源和许可清单 **UNKNOWN** | 如使用系统字体，仅记录依赖与覆盖测试；如随包分发字体，需单独确认再分发许可、版权及名称限制，不能从系统随意拷贝字体 |
| 转写模型 `Systran/faster-whisper-base.en` | [上游模型卡](https://huggingface.co/Systran/faster-whisper-base.en) 标注 MIT；开发候选 revision 见下文 | 固定 revision 对应的许可正文、权重／分词文件哈希、原始模型来源、下载量和校验记录 |
| 翻译模型 `Helsinki-NLP/opus-mt-en-zh` | [上游模型卡](https://huggingface.co/Helsinki-NLP/opus-mt-en-zh) 标注 Apache 2.0；固定 revision 对应许可全文尚需归档 | 权重及分词文件的许可、来源、哈希，转换所用工具版本、参数和转换产物校验；不假定模型权重与工具使用同一许可 |

当前源码中，转写 `base.en` 固定 revision 为 `3d3d5dee26484f91867d81cb899cfcf72b96be6c`，翻译固定 revision 为 `408d9bc410a388e1d9aef112a2daba955b945255`；最终发布须与模型管理器常量重新核对。模型首启下载，不默认全部塞入 DMG；变更官方源、设置镜像或再分发之前先核实这些文件的许可及完整性。上游模型卡可读不代表该地区可以成功下载全部权重。

## 分发、更新与费用

当前 DMG 下载渠道、国内外镜像、更新 manifest 托管位置均为 **UNKNOWN／未部署验证**，本文不提供占位下载地址。发行负责人确定 HTTPS 渠道后才设置 `SUBTRANS_DESKTOP_UPDATE_URL`；模型替代来源如需 `SUBTRANS_DESKTOP_MODEL_ENDPOINT`，同样须先完成许可及两地下载验证。缺少渠道时应用应显示“未配置更新渠道”。

更新 manifest 按实施契约包含 `version`、`release_notes`、`published_at`，以及 `platforms.macos-arm64` 下的 `url`、`sha256`、`minimum_macos`。只填写真实稳定版本、最终 DMG 的 HTTPS 地址和装订后计算的哈希；最低系统版本应来自验收。应用只检查并引导下载 DMG，用户自行安装，不能声称已经实现下载文件的自动校验、自动替换或自动重启。先验证实际资产和下载页，再发布指向它们的 manifest，避免用户收到失效更新入口。

| 用途 | 已核实金额与来源 | 本项目实际支出／状态 |
| --- | --- | --- |
| Apple Developer Program 会员，用于 Developer ID 发行及相关能力 | 官方为 **99 USD／会员年**，地区价格可能不同；[Apple 注册页](https://developer.apple.com/programs/enroll/)于 2026-10-17 只读核实 | **UNKNOWN**：会员是否可复用、当地含税金额及实际应付金额未核实；未购买 |
| 签名与公证相关额外服务 | 单独收费金额 **UNKNOWN**；不假定需要购买第三方签名服务 | **UNKNOWN**；账号、公证权限和钥匙串可用性未核实 |
| DMG／模型／更新信息托管、流量、域名与镜像 | 供应商、来源和费用 **UNKNOWN** | **UNKNOWN**；未选择、未订购或上传 |
| 目标测试设备及网络环境 | 复用／租用安排和费用 **UNKNOWN** | **UNKNOWN**；不能把当前开发机当成已具备 M1／8GB 验收环境 |

支出按具体用途、金额、一次性／周期性分别提交用户决定。金额核实仅是报价信息，不代表实际扣费或授权；所有未知项都应在发行记录中关闭或明确保持阻塞。
