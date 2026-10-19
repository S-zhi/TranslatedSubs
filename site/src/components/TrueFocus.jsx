'use client';

import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { gsap } from 'gsap';
import './TrueFocus.css';

// React Bits True Focus visual, adapted to the site's existing GSAP runtime.
export default function TrueFocus({
  sentence = 'True Focus',
  blurAmount = 1.5,
  animationDuration = 0.45,
  pauseBetweenAnimations = 2.4,
  paused = false,
  reducedMotion = false,
}) {
  const words = sentence.split(' ').filter(Boolean);
  const containerRef = useRef(null);
  const wordRefs = useRef([]);
  const cornerRefs = useRef([]);
  const [currentIndex, setCurrentIndex] = useState(0);
  const [hoverIndex, setHoverIndex] = useState(null);
  const [visible, setVisible] = useState(false);
  const [pageVisible, setPageVisible] = useState(() => !document.hidden);
  const activeIndex = (hoverIndex ?? currentIndex) % Math.max(words.length, 1);

  useEffect(() => {
    const observer = new IntersectionObserver(entries => {
      setVisible(entries.some(entry => entry.isIntersecting));
    });
    observer.observe(containerRef.current);
    const onVisibility = () => setPageVisible(!document.hidden);
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      observer.disconnect();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, []);

  useEffect(() => {
    if (paused || reducedMotion || !visible || !pageVisible || hoverIndex !== null || words.length < 2) return;
    const interval = window.setInterval(() => {
      setCurrentIndex(index => (index + 1) % words.length);
    }, (animationDuration + pauseBetweenAnimations) * 1000);
    return () => window.clearInterval(interval);
  }, [paused, reducedMotion, visible, pageVisible, hoverIndex, words.length, animationDuration, pauseBetweenAnimations]);

  useLayoutEffect(() => {
    const container = containerRef.current;
    const word = wordRefs.current[activeIndex];
    if (!container || !word || reducedMotion) return;
    let active = true;
    let tween;
    const measure = (immediate = false) => {
      if (!active) return;
      const box = container.getBoundingClientRect();
      const rect = word.getBoundingClientRect();
      const gap = 8;
      const size = 13;
      const x = rect.left - box.left;
      const y = rect.top - box.top;
      const positions = [
        [x - gap, y - gap],
        [x + rect.width + gap - size, y - gap],
        [x - gap, y + rect.height + gap - size],
        [x + rect.width + gap - size, y + rect.height + gap - size],
      ];
      tween?.kill();
      tween = gsap.to(cornerRefs.current, {
        x: index => positions[index][0],
        y: index => positions[index][1],
        opacity: 1,
        duration: paused || immediate ? 0 : animationDuration,
        ease: 'power3.inOut',
        overwrite: true,
      });
    };
    measure();
    const align = () => measure(true);
    let initialObservation = true;
    const observer = new ResizeObserver(() => {
      if (initialObservation) {
        initialObservation = false;
        return;
      }
      align();
    });
    observer.observe(container);
    wordRefs.current.forEach(node => node && observer.observe(node));
    window.addEventListener('resize', align);
    if (document.fonts?.status !== 'loaded') document.fonts?.ready.then(align);
    return () => {
      active = false;
      observer.disconnect();
      window.removeEventListener('resize', align);
      tween?.kill();
    };
  }, [activeIndex, sentence, animationDuration, paused, reducedMotion]);

  return (
    <span
      className={`true-focus${reducedMotion ? ' true-focus--still' : ''}${paused ? ' true-focus--paused' : ''}`}
      ref={containerRef}
      aria-hidden="true"
      onMouseLeave={() => setHoverIndex(null)}
    >
      {words.map((word, index) => (
        <span
          className={`true-focus__word${index === activeIndex ? ' is-focused' : ''}`}
          key={`${word}-${index}`}
          ref={node => { wordRefs.current[index] = node; }}
          style={{ '--focus-blur': `${blurAmount}px`, '--focus-duration': `${animationDuration}s` }}
          onMouseEnter={() => { if (!paused && !reducedMotion) setHoverIndex(index); }}
        >
          {word}
        </span>
      ))}
      {['top-left', 'top-right', 'bottom-left', 'bottom-right'].map((corner, index) => (
        <span
          className={`true-focus__corner true-focus__corner--${corner}`}
          key={corner}
          ref={node => { cornerRefs.current[index] = node; }}
        />
      ))}
    </span>
  );
}
