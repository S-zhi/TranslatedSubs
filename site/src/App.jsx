import { useEffect, useState } from 'react';
import {
  ArrowUpRightIcon,
  FileVideoIcon,
  GithubLogoIcon,
  MoonIcon,
  SunIcon,
  TiktokLogoIcon,
  XLogoIcon,
  YoutubeLogoIcon,
} from '@phosphor-icons/react';
import BackgroundStage from './components/BackgroundStage.jsx';
import HeroPreview from './components/HeroPreview.jsx';
import VideoSection from './components/VideoSection.jsx';
import AdvantagesSection from './components/AdvantagesSection.jsx';
import GetStartedSection from './components/GetStartedSection.jsx';
import SiteFooter from './components/SiteFooter.jsx';
import { GUIDE_URL as GUIDE, REPOSITORY_URL as REPOSITORY } from './content/site-links.js';
import useCloudTrial from './hooks/use-cloud-trial.js';
import { getCloudTrialAction } from './lib/cloud-trial-controller.js';

function useMediaQuery(query) {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);

  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener('change', update);
    return () => media.removeEventListener('change', update);
  }, [query]);

  return matches;
}

export default function App() {
  const trial = useCloudTrial();
  const systemDark = useMediaQuery('(prefers-color-scheme: dark)');
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)');
  const compact = useMediaQuery('(max-width: 767px)');
  const [themeOverride, setThemeOverride] = useState(null);
  const [videoPlaying, setVideoPlaying] = useState(false);
  const dark = themeOverride === null ? systemDark : themeOverride === 'dark';
  const trialAction = getCloudTrialAction(trial);

  const scrollToTrial = () => {
    document.getElementById('cloud-start')?.scrollIntoView({
      behavior: reducedMotion ? 'instant' : 'smooth',
      block: 'start',
    });
  };

  const startOrCheckTrial = () => {
    if (trial.configured === null && !trial.error) void trial.createTrial();
    else if (trialAction === 'create') void trial.createTrial();
    else if (trialAction === 'recheck') void trial.recheckTrial();
    scrollToTrial();
  };

  useEffect(() => {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', dark ? '#0c0a12' : '#f7f6fa');
  }, [dark]);

  return (
    <div className="product-page">
      <BackgroundStage dark={dark} compact={compact} reducedMotion={reducedMotion} paused={reducedMotion || videoPlaying} />

      <div className="landing" id="overview">
        <a className="skip-link" href="#main">跳到主要内容</a>
        <header className="site-header">
          <a className="brand" href="#overview" aria-label="TranslatedSubs 首页">
            <img src={`${import.meta.env.BASE_URL}brand.svg`} width="34" height="34" alt="" />
            <span>Translated<span className="brand__ending">Subs</span></span>
          </a>

          <nav className="site-nav" aria-label="主导航">
            <a className="site-nav__current" href="#overview">产品总览</a>
            <a href="#film">宣传视频</a>
            <a href="#advantages">核心优势</a>
            <a href={GUIDE} target="_blank" rel="noreferrer">
              使用文档 <ArrowUpRightIcon size={14} aria-hidden="true" />
            </a>
          </nav>

          <button
            className="icon-button theme-toggle"
            type="button"
            onClick={() => setThemeOverride(dark ? 'light' : 'dark')}
            aria-label={dark ? '切换浅色主题' : '切换深色主题'}
            title={dark ? '切换浅色主题' : '切换深色主题'}
          >
            {dark ? <SunIcon size={19} aria-hidden="true" /> : <MoonIcon size={19} aria-hidden="true" />}
          </button>
        </header>

        <main className="hero" id="main" tabIndex={-1} aria-label="产品介绍">
          <div className="hero__content">
            <p className="hero__eyebrow">TranslatedSubs视频字幕工作台</p>
            <h1>
              <span>让好内容，</span>
              <span className="hero__accent">跨越语言。</span>
            </h1>
            <p className="hero__description">
              从一段视频开始，完成转写、翻译与校对。<br className="desktop-break" />
              让每一句表达，都有被理解的可能。
            </p>
            <div className="hero__actions">
              {trialAction === 'open' ? (
                <a
                  className="button button--primary"
                  href={trial.url}
                  target="_blank"
                  rel="noreferrer"
                  onClick={(event) => {
                    if (!trial.canOpenTrial()) event.preventDefault();
                  }}
                >
                  打开我的工作台 <ArrowUpRightIcon size={19} weight="bold" aria-hidden="true" />
                </a>
              ) : (
                <button
                  className="button button--primary"
                  type="button"
                  onClick={startOrCheckTrial}
                >
                  {trial.configured === null && !trial.error
                    ? '快速使用'
                    : trialAction === 'wait'
                      ? '查看准备进度'
                      : trialAction === 'recheck'
                        ? '检查云端试用状态'
                        : '快速使用'}
                  <ArrowUpRightIcon size={19} weight="bold" aria-hidden="true" />
                </button>
              )}
              <a className="button button--secondary" href={REPOSITORY} target="_blank" rel="noreferrer">
                <GithubLogoIcon size={20} weight="bold" aria-hidden="true" /> GitHub
              </a>
            </div>
            <a
              className="hero__cloud-guide"
              href="#cloud-start"
              onClick={(event) => {
                event.preventDefault();
                scrollToTrial();
              }}
            >
              无需账号或终端命令 · 查看试用状态
            </a>
          </div>
          <HeroPreview reducedMotion={reducedMotion} paused={videoPlaying} />
        </main>

        <div className="hero-info">
          <div className="hero-info__sources">
            <p className="hero-info__label">从你常看的平台开始</p>
            <ul className="hero-info__platforms" aria-label="支持的内容来源示例">
              <li><YoutubeLogoIcon size={23} weight="fill" aria-hidden="true" /><span>YouTube</span></li>
              <li><TiktokLogoIcon size={21} weight="fill" aria-hidden="true" /><span>TikTok</span></li>
              <li><XLogoIcon size={19} aria-hidden="true" /><span>X / Twitter</span></li>
              <li><FileVideoIcon size={22} aria-hidden="true" /><span>本地视频</span></li>
            </ul>
          </div>
          <div className="hero-info__workflow">
            <p className="hero-info__label">一段视频，一条完整流程</p>
            <ol className="hero-info__steps" aria-label="字幕处理流程">
              <li>导入</li>
              <li>转写</li>
              <li>翻译</li>
              <li>校对</li>
              <li>导出</li>
            </ol>
            <p className="hero-info__note">本地模型与云端服务，按需组合。</p>
          </div>
        </div>
      </div>

      <VideoSection reducedMotion={reducedMotion} onPlaybackChange={setVideoPlaying} />
      <AdvantagesSection reducedMotion={reducedMotion} motionPaused={reducedMotion} />
      <GetStartedSection trial={trial} onStartTrial={trial.createTrial} />
      <SiteFooter />
    </div>
  );
}
