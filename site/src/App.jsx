import { useEffect, useState } from 'react';
import {
  ArrowRightIcon,
  ArrowUpRightIcon,
  GithubLogoIcon,
  MoonIcon,
  PauseIcon,
  PlayIcon,
  SunIcon,
} from '@phosphor-icons/react';
import BackgroundStage from './components/BackgroundStage.jsx';
import VideoSection from './components/VideoSection.jsx';
import AdvantagesSection from './components/AdvantagesSection.jsx';
import GetStartedSection from './components/GetStartedSection.jsx';
import SiteFooter from './components/SiteFooter.jsx';
import { GUIDE_URL as GUIDE, REPOSITORY_URL as REPOSITORY } from './content/site-links.js';

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
  const systemDark = useMediaQuery('(prefers-color-scheme: dark)');
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)');
  const compact = useMediaQuery('(max-width: 767px)');
  const [themeOverride, setThemeOverride] = useState(null);
  const [paused, setPaused] = useState(false);
  const [videoPlaying, setVideoPlaying] = useState(false);
  const dark = themeOverride === null ? systemDark : themeOverride === 'dark';
  const motionPaused = paused || reducedMotion;
  const toggleMotion = () => setPaused(value => !value);

  useEffect(() => {
    document.documentElement.dataset.theme = dark ? 'dark' : 'light';
    document.querySelector('meta[name="theme-color"]')?.setAttribute('content', dark ? '#0c0a12' : '#f7f6fa');
  }, [dark]);

  return (
    <div className="product-page">
      <BackgroundStage dark={dark} compact={compact} reducedMotion={reducedMotion} paused={motionPaused || videoPlaying} />

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
            <p className="hero__eyebrow">开源视频字幕工作台</p>
            <h1>
              <span>让好内容，</span>
              <span className="hero__accent">跨越语言。</span>
            </h1>
            <p className="hero__description">
              从一段视频开始，完成转写、翻译与校对。<br className="desktop-break" />
              让每一句表达，都有被理解的可能。
            </p>
            <div className="hero__actions">
              <a className="button button--primary" href={GUIDE} target="_blank" rel="noreferrer">
                开始使用 <ArrowRightIcon size={19} weight="bold" aria-hidden="true" />
              </a>
              <a className="button button--secondary" href={REPOSITORY} target="_blank" rel="noreferrer">
                <GithubLogoIcon size={20} weight="bold" aria-hidden="true" /> GitHub
              </a>
            </div>
          </div>
        </main>

        <div className="scene-controls">
          <button
            className="motion-toggle"
            type="button"
            onClick={toggleMotion}
            aria-label={motionPaused ? '播放背景动效' : '暂停背景动效'}
            aria-pressed={motionPaused}
            disabled={reducedMotion}
            title={reducedMotion ? '已跟随系统减少动态效果设置' : undefined}
          >
            {motionPaused ? <PlayIcon size={13} weight="fill" aria-hidden="true" /> : <PauseIcon size={13} weight="fill" aria-hidden="true" />}
            <span>{motionPaused ? '动效已暂停' : '暂停动效'}</span>
          </button>
        </div>
      </div>

      <VideoSection
        reducedMotion={reducedMotion}
        motionPaused={motionPaused}
        onToggleMotion={toggleMotion}
        onPlaybackChange={setVideoPlaying}
      />
      <AdvantagesSection reducedMotion={reducedMotion} motionPaused={motionPaused} />
      <GetStartedSection />
      <SiteFooter />
    </div>
  );
}
