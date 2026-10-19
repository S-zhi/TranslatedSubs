import { ArrowUpRightIcon, CheckIcon } from '@phosphor-icons/react';
import { CONTACT_URL, REPOSITORY_URL } from '../content/site-links.js';
import CloudQuickStart from './CloudQuickStart.jsx';
import './GetStartedSection.css';

export default function GetStartedSection({ trial, onStartTrial }) {
  return (
    <section className="get-started" id="get-started" aria-labelledby="get-started-title">
      <div className="get-started__inner">
        <header className="get-started__heading">
          <h2 id="get-started-title">选择你的开始方式。</h2>
          <p>先在云端体验，再选择自己的部署方式。</p>
        </header>

        <CloudQuickStart trial={trial} onStart={onStartTrial} />

        <div className="get-started__options">
          <article className="start-option">
            <h3>在自己的环境里运行</h3>
            <p>从开源代码开始，把视频字幕工作台部署到自己的电脑或服务器。</p>
            <ul>
              <li><CheckIcon size={19} aria-hidden="true" />获取源码与部署文档</li>
              <li><CheckIcon size={19} aria-hidden="true" />按需配置本地模型与云端服务</li>
              <li><CheckIcon size={19} aria-hidden="true" />自主管理处理环境与任务文件</li>
            </ul>
            <a className="button button--primary start-option__action" href={REPOSITORY_URL} target="_blank" rel="noreferrer">
              前往 GitHub <ArrowUpRightIcon size={18} aria-hidden="true" />
            </a>
          </article>

          <article className="start-option">
            <h3>一起讨论更适合的方案</h3>
            <p>通过 GitHub Discussions 与维护者交流，让部署、模型选择和处理流程更贴合实际需求。</p>
            <ul>
              <li><CheckIcon size={19} aria-hidden="true" />沟通私有化部署与模型选型</li>
              <li><CheckIcon size={19} aria-hidden="true" />讨论批量处理与工具集成</li>
              <li><CheckIcon size={19} aria-hidden="true" />交流具体场景与功能扩展需求</li>
            </ul>
            <a className="button button--secondary start-option__action" href={CONTACT_URL} target="_blank" rel="noreferrer">
              联系我们 <ArrowUpRightIcon size={18} aria-hidden="true" />
            </a>
          </article>
        </div>
      </div>
    </section>
  );
}
