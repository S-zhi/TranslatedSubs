import { useEffect, useRef, useState } from 'react';
import { ArrowCounterClockwiseIcon, PauseIcon, PlayIcon } from '@phosphor-icons/react';
import SplitText from './SplitText.jsx';
import './VideoSection.css';
const VIDEO = {
  src: `${import.meta.env.BASE_URL}media/promo-placeholder.mp4`,
  poster: `${import.meta.env.BASE_URL}media/promo-placeholder.jpg`,
};

export default function VideoSection({ reducedMotion, motionPaused, onToggleMotion, onPlaybackChange }) {
  const videoRef = useRef(null);
  const [playback, setPlayback] = useState('idle');
  const [error, setError] = useState('');
  const watching = playback === 'playing' || playback === 'loading';
  const backgroundPaused = motionPaused || watching;

  useEffect(() => {
    onPlaybackChange(watching);
    return () => onPlaybackChange(false);
  }, [watching, onPlaybackChange]);

  useEffect(() => {
    const video = videoRef.current;
    const videoObserver = new IntersectionObserver(entries => {
      if (entries.every(entry => entry.intersectionRatio < 0.1)
        && !document.fullscreenElement && !video.webkitDisplayingFullscreen) {
        video.pause();
      }
    }, { threshold: [0, 0.1] });
    const onVisibility = () => {
      if (document.hidden) video.pause();
    };
    videoObserver.observe(video);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      videoObserver.disconnect();
      document.removeEventListener('visibilitychange', onVisibility);
      video.pause();
    };
  }, []);

  const startPlayback = async () => {
    const video = videoRef.current;
    if (!video || watching) return;
    setError('');
    setPlayback('loading');
    if (video.error) video.load();
    if (video.ended) video.currentTime = 0;
    try {
      await video.play();
    } catch (cause) {
      if (cause.name === 'AbortError') return;
      setPlayback('error');
      setError('视频暂时无法播放，请重试。');
    }
  };

  return (
    <section className="video-section" id="film" aria-labelledby="film-title">
      <div className="video-section__inner">
        <header className="video-section__heading" id="film-title">
          <SplitText
            key={reducedMotion ? 'still' : 'animated'}
            text="让理解，发生在每一帧。"
            tag="h2"
            className="film-title"
            effect="rise"
            splitType="chars"
            delay={45}
            duration={0.9}
            staggerFrom="start"
            trigger="view"
            threshold={0.15}
            rootMargin="-60px"
            hoverEffect={reducedMotion ? 'none' : 'lift'}
            loop={false}
            textAlign="center"
          />
          <p>从导入视频，到输出双语字幕。看看好内容如何跨越语言。</p>
        </header>

        <figure className="film">
          <div className={`film__screen${watching ? ' is-playing' : ''}`}>
            <video
              id="promo-video"
              ref={videoRef}
              src={VIDEO.src}
              poster={VIDEO.poster}
              width="960"
              height="540"
              preload="none"
              playsInline
              controls={playback !== 'idle' && playback !== 'error'}
              aria-label="产品宣传片占位视频：风中摇曳的花朵，无对白"
              aria-describedby="film-caption"
              onPlay={() => setPlayback('loading')}
              onPlaying={() => setPlayback('playing')}
              onWaiting={() => setPlayback('loading')}
              onPause={() => {
                if (!videoRef.current?.ended) {
                  setPlayback(current => current === 'error' || current === 'idle' ? current : 'paused');
                }
              }}
              onEnded={() => setPlayback('ended')}
              onError={() => {
                setPlayback('error');
                setError('视频加载失败，请检查连接后重试。');
              }}
            />

            {(playback === 'idle' || playback === 'ended') && (
              <button
                className="film__play"
                type="button"
                onClick={startPlayback}
                aria-label={playback === 'ended' ? '重新播放占位视频' : '播放宣传片占位视频'}
                aria-controls="promo-video"
              >
                <span className="film__play-icon">
                  {playback === 'ended'
                    ? <ArrowCounterClockwiseIcon size={28} aria-hidden="true" />
                    : <PlayIcon size={28} weight="fill" aria-hidden="true" />}
                </span>
                <span>{playback === 'ended' ? '重新播放' : '播放视频'}</span>
              </button>
            )}

            {playback === 'loading' && <p className="film__loading" role="status">正在加载视频…</p>}
            {playback === 'error' && (
              <div className="film__error">
                <p role="alert">{error}</p>
                <button className="button button--primary" type="button" onClick={startPlayback}>
                  <ArrowCounterClockwiseIcon size={18} aria-hidden="true" /> 重试播放
                </button>
              </div>
            )}
          </div>
          <figcaption className="film__caption" id="film-caption">
            <span>宣传视频</span>
            <span>当前为占位素材，正式宣传片即将替换。</span>
          </figcaption>
        </figure>
      </div>

      <div className="scene-controls video-section__controls">
        <button
          className="motion-toggle"
          type="button"
          onClick={onToggleMotion}
          aria-label={backgroundPaused ? '播放粒子背景动效' : '暂停粒子背景动效'}
          aria-pressed={backgroundPaused}
          disabled={reducedMotion || watching}
          title={watching ? '播放视频时背景自动暂停' : reducedMotion ? '已跟随系统减少动态效果设置' : undefined}
        >
          {backgroundPaused
            ? <PlayIcon size={13} weight="fill" aria-hidden="true" />
            : <PauseIcon size={13} weight="fill" aria-hidden="true" />}
          <span>{backgroundPaused ? '背景已暂停' : '暂停动效'}</span>
        </button>
      </div>
    </section>
  );
}
