import TrueFocus from './TrueFocus.jsx';
import './AdvantagesSection.css';

export default function AdvantagesSection({ reducedMotion, motionPaused }) {
  return (
    <section className="advantages" id="advantages" aria-labelledby="advantages-title">
      <div className="advantages__inner">
        <header className="advantages__heading">
          <h2 id="advantages-title" aria-label="可插拔，本地与云端，多平台，可扩展">
            <TrueFocus
              sentence="可插拔 本地与云端 多平台 可扩展"
              blurAmount={1.5}
              animationDuration={0.45}
              pauseBetweenAnimations={2.4}
              paused={motionPaused}
              reducedMotion={reducedMotion}
            />
          </h2>
          <p>把能力拆开，把流程连起来。按自己的内容、设备和习惯，自由组合。</p>
        </header>

        <div className="advantages__grid">
          <article className="advantage">
            <h3>插件式设计，<strong>按需插拔。</strong></h3>
            <p>下载、转写、翻译、配音与封装，拆成独立的处理能力。为不同任务选择插件实例，替换其中一环，组合适合自己的工作流。</p>
            <ul className="advantage__examples" aria-label="插件系统特点">
              <li>独立处理阶段</li>
              <li>多实例配置</li>
              <li>按任务选择</li>
            </ul>
          </article>

          <article className="advantage">
            <h3>模型选择，<strong>不止一种。</strong></h3>
            <p>本地 Whisper 转写、Kokoro TTS 配音、本地英译中，也能连接云端转写与翻译服务。按设备、语言和使用成本，选择合适的处理方式。</p>
            <ul className="advantage__examples" aria-label="模型与服务">
              <li>Whisper</li>
              <li>Kokoro TTS</li>
              <li>本地翻译</li>
              <li>云端 API</li>
            </ul>
          </article>

          <article className="advantage">
            <h3>内容来源，<strong>不止一个平台。</strong></h3>
            <p>从 YouTube、Vimeo、TikTok、X 等平台链接开始，也支持导入本地视频。把不同来源的内容，交给同一个字幕工作台。</p>
            <ul className="advantage__examples" aria-label="支持的内容来源示例">
              <li>YouTube</li>
              <li>Vimeo</li>
              <li>TikTok</li>
              <li>X / Twitter</li>
              <li>本地文件</li>
            </ul>
            <p className="advantage__note">平台可用性受网络、登录状态和来源站点限制影响。</p>
          </article>

          <article className="advantage">
            <h3>从字幕出发，<strong>把流程继续延伸。</strong></h3>
            <p>从人声分离、语音配音到字幕封装，按任务组合处理阶段；通过 API 或 MCP 创建字幕任务、查询进度，让媒体处理接入已有工具。</p>
            <ul className="advantage__examples" aria-label="流程扩展方向">
              <li>人声分离</li>
              <li>语音配音</li>
              <li>字幕封装</li>
              <li>API / MCP</li>
            </ul>
          </article>
        </div>
      </div>
    </section>
  );
}
