import { CLOUD_TRIAL_ENABLED } from '../lib/build-flags.js';

export const REPOSITORY_URL = 'https://github.com/S-zhi/TranslatedSubs';
export const GUIDE_URL = `${REPOSITORY_URL}/blob/main/docs/local-quick-start.md`;
export const CONTACT_URL = `${REPOSITORY_URL}/discussions`;

// 云端试用只服务官网部署。自托管构建（SUBTRANS_ENABLE_CLOUD_TRIAL=0）里
// #cloud-start 锚点不存在，页脚保留它就是一个死链，因此按构建开关过滤。
const cloudTrialLinks = CLOUD_TRIAL_ENABLED
  ? [{ label: '云端试用', href: '#cloud-start' }]
  : [];

export const FOOTER_GROUPS = [
  {
    title: '开始部署',
    links: [
      ...cloudTrialLinks,
      { label: '本地快速启动', href: GUIDE_URL },
      { label: 'Linux 部署', href: `${REPOSITORY_URL}/blob/main/docs/quick-start-linux.md` },
      { label: 'Docker 部署', href: `${REPOSITORY_URL}/blob/main/README.md#docker-quick-start` },
    ],
  },
  {
    title: '使用指南',
    links: [
      { label: '插件实例', href: `${REPOSITORY_URL}/blob/main/docs/plugin-instances.md` },
      { label: '本地英译中', href: `${REPOSITORY_URL}/blob/main/docs/local-translation.md` },
      { label: '转写服务', href: `${REPOSITORY_URL}/blob/main/docs/transcriber-service.md` },
      { label: '语音配音', href: `${REPOSITORY_URL}/blob/main/docs/tts.md` },
    ],
  },
  {
    title: '开发与集成',
    links: [
      { label: '文档目录', href: `${REPOSITORY_URL}/blob/main/docs/README.md` },
      { label: 'MCP Server', href: `${REPOSITORY_URL}/blob/main/docs/mcp-server.md` },
      { label: 'MCP Agent 指南', href: `${REPOSITORY_URL}/blob/main/docs/mcp-agent-guide.md` },
    ],
  },
  {
    title: '开源项目',
    links: [
      { label: '问题反馈', href: `${REPOSITORY_URL}/issues` },
      { label: '版本发布', href: `${REPOSITORY_URL}/releases` },
      { label: '贡献指南', href: `${REPOSITORY_URL}/blob/main/.github/CONTRIBUTING.md` },
      { label: 'MIT 许可证', href: `${REPOSITORY_URL}/blob/main/LICENSE` },
    ],
  },
];
