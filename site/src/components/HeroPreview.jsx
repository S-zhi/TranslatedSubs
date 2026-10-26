import { useEffect, useRef } from 'react';
import { gsap } from 'gsap';
import { useGSAP } from '@gsap/react';
import { ArrowRightIcon, CheckIcon, SubtitlesIcon } from '@phosphor-icons/react';
import './HeroPreview.css';

const WAVEFORM = [10, 18, 30, 20, 14, 26, 42, 32, 18, 24, 40, 54, 36, 22, 32, 48, 62, 42, 28, 44, 56, 38, 24, 34, 50, 66, 46, 30, 42, 58, 38, 22, 32, 48, 34, 18, 28, 40, 24, 16, 30, 20, 12, 18];
const CAPTIONS = [
  { lines: ['Good ideas deserve', 'to be understood.'], translation: '好想法，值得被理解。', time: '00:00.00 — 00:07.50' },
  { lines: ['Every voice carries', 'a story to share.'], translation: '每一种声音，都有故事可分享。', time: '00:07.50 — 00:15.00' },
  { lines: ['Keep the meaning.', 'Reach a little further.'], translation: '保留表达，让内容走得更远。', time: '00:15.00 — 00:22.50' },
  { lines: ['Your content,', 'in another language.'], translation: '你的内容，用另一种语言被理解。', time: '00:22.50 — 00:30.00' },
];
const CUE_DURATION = 7.5;

gsap.registerPlugin(useGSAP);

export default function HeroPreview({ reducedMotion = false, paused = false }) {
  const previewRef = useRef(null);
  const pausedRef = useRef(paused);
  const syncRef = useRef(null);

  useEffect(() => {
    pausedRef.current = paused;
    syncRef.current?.();
  }, [paused]);

  useGSAP(() => {
    if (reducedMotion) return;
    const root = previewRef.current;
    const cues = gsap.utils.toArray('.hero-preview__cue', root);
    const clips = gsap.utils.toArray('.hero-preview__clip', root);
    const stages = gsap.utils.toArray('.hero-preview__stages > span', root);
    const playhead = root.querySelector('.hero-preview__playhead');
    const demo = gsap.timeline({ repeat: -1, paused: true });

    demo.set(clips.map(clip => clip.firstElementChild), { scaleX: 0 }, 0);
    demo.fromTo(playhead, { left: '0%' }, {
      left: '100%', duration: CAPTIONS.length * CUE_DURATION, ease: 'none',
    }, 0);

    cues.forEach((cue, index) => {
      const start = index * CUE_DURATION;
      const original = cue.querySelector('.hero-preview__original');
      const translation = cue.querySelector('.hero-preview__translation');
      demo
        .set(cues, { autoAlpha: 0 }, start)
        .set(cue, { autoAlpha: 1, y: 0 }, start)
        .set([original, translation], { autoAlpha: 0, y: 8 }, start)
        .set(clips, { borderColor: 'var(--border)', opacity: 0.45 }, start)
        .set(clips[index], { borderColor: 'var(--accent)', opacity: 1 }, start)
        .set(stages, { opacity: 0.3 }, start)
        .to(original, {
          autoAlpha: 1, y: 0, duration: 0.55, ease: 'power2.out',
        }, start + 0.15)
        .to(translation, {
          autoAlpha: 1, y: 0, duration: 0.65, ease: 'power2.out',
        }, start + 1.25)
        .to(stages[0], { opacity: 1, duration: 0.3 }, start + 0.15)
        .to(stages[1], { opacity: 1, duration: 0.3 }, start + 1.25)
        .to(stages[2], { opacity: 1, duration: 0.3 }, start + 2.8)
        .fromTo(clips[index].firstElementChild, { scaleX: 0 }, {
          scaleX: 1, duration: CUE_DURATION, ease: 'none', immediateRender: false,
        }, start)
        .to(cue, { autoAlpha: 0, y: -5, duration: 0.35 }, start + CUE_DURATION - 0.4);
    });

    const waves = gsap.fromTo(root.querySelectorAll('.hero-preview__waveform span'), {
      scaleY: index => 0.3 + (index % 4) * 0.1,
      opacity: 0.3,
    }, {
      scaleY: index => 0.75 + (index % 5) * 0.1,
      opacity: 0.85,
      duration: 0.55,
      stagger: { each: 0.025, from: 'center' },
      repeat: -1,
      yoyo: true,
      ease: 'sine.inOut',
      paused: true,
    });

    let visible = false;
    const sync = () => {
      const shouldPlay = visible && !document.hidden && !pausedRef.current;
      demo.paused(!shouldPlay);
      waves.paused(!shouldPlay);
    };
    const observer = new IntersectionObserver(entries => {
      visible = entries.some(entry => entry.isIntersecting && entry.intersectionRatio >= 0.15);
      sync();
    }, { threshold: [0, 0.15] });
    observer.observe(root);
    document.addEventListener('visibilitychange', sync);
    syncRef.current = sync;

    return () => {
      observer.disconnect();
      document.removeEventListener('visibilitychange', sync);
      syncRef.current = null;
    };
  }, { scope: previewRef, dependencies: [reducedMotion], revertOnUpdate: true });

  return (
    <figure className="hero-preview" ref={previewRef} aria-labelledby="hero-preview-title" aria-describedby="hero-preview-description">
      <p className="hero-preview__accessible" id="hero-preview-description">
        双语字幕效果示意：英文“Good ideas deserve to be understood.”，中文“好想法，值得被理解。”
        波形与时间轴展示语音转写、双语翻译和字幕导出的过程。
      </p>
      <div className="hero-preview__window">
        <header className="hero-preview__header">
          <span className="hero-preview__title" id="hero-preview-title">
            <SubtitlesIcon size={20} aria-hidden="true" /> 双语字幕预览
          </span>
          <span className="hero-preview__sample">效果示意</span>
        </header>

        <div className="hero-preview__scene">
          <div className="hero-preview__waveform" aria-hidden="true">
            {WAVEFORM.map((height, index) => (
              <span key={index} style={{ height: `${height}px` }} />
            ))}
          </div>
          <div className="hero-preview__languages">
            <span>English</span>
            <ArrowRightIcon size={14} aria-hidden="true" />
            <span>简体中文</span>
          </div>
          <div className="hero-preview__captions" aria-hidden="true">
            {CAPTIONS.map(caption => (
              <div className="hero-preview__cue" key={caption.time}>
                <p className="hero-preview__original" lang="en">{caption.lines[0]}<br />{caption.lines[1]}</p>
                <p className="hero-preview__translation">{caption.translation}</p>
                <span className="hero-preview__time">{caption.time}</span>
              </div>
            ))}
          </div>
        </div>

        <div className="hero-preview__timeline" aria-hidden="true">
          <div className="hero-preview__ruler"><span>00:00</span><span>00:15</span><span>00:30</span></div>
          <div className="hero-preview__track">
            {CAPTIONS.map(caption => (
              <span className="hero-preview__clip" key={caption.time}><span /></span>
            ))}
            <i className="hero-preview__playhead" />
          </div>
        </div>

        <div className="hero-preview__stages" aria-label="从转写到字幕导出">
          <span><CheckIcon size={14} aria-hidden="true" />语音转写</span>
          <span><CheckIcon size={14} aria-hidden="true" />双语翻译</span>
          <span><CheckIcon size={14} aria-hidden="true" />字幕导出</span>
        </div>
      </div>
      <figcaption className="hero-preview__caption">
        <span>保留原声，也读懂另一种语言。</span>
        <a href="#film">看看如何实现 <ArrowRightIcon size={15} aria-hidden="true" /></a>
      </figcaption>
    </figure>
  );
}
