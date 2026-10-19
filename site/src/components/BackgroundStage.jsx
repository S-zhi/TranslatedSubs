'use client';

import { lazy, Suspense, useRef, useState } from 'react';
import { gsap } from 'gsap';
import { ScrollTrigger } from 'gsap/ScrollTrigger';
import { useGSAP } from '@gsap/react';
import GhostFibers from './GhostFibers.jsx';
import './BackgroundStage.css';

const Particles = lazy(() => import('./Particles.jsx'));
gsap.registerPlugin(ScrollTrigger, useGSAP);

export default function BackgroundStage({ dark, compact, reducedMotion, paused }) {
  const stageRef = useRef(null);
  const modeRef = useRef('fibers');
  const [mode, setMode] = useState('fibers');

  useGSAP(() => {
    const fibers = stageRef.current.querySelector('.ambient-fibers');
    const particles = stageRef.current.querySelector('.ambient-particles');
    if (reducedMotion) {
      gsap.set(fibers, { opacity: 0.45 });
      gsap.set(particles, { opacity: 0.5 });
      return;
    }

    gsap.timeline({
      scrollTrigger: {
        trigger: document.getElementById('overview'),
        start: 'top top',
        end: 'bottom 12%',
        scrub: true,
        invalidateOnRefresh: true,
        onUpdate: self => {
          const next = self.progress > 0.995 ? 'particles' : self.progress < 0.005 ? 'fibers' : 'blend';
          if (next !== modeRef.current) {
            modeRef.current = next;
            setMode(next);
          }
        },
      },
    })
      .fromTo(fibers, { opacity: 1 }, { opacity: 0, duration: 1, ease: 'sine.inOut' }, 0)
      .fromTo(particles, { opacity: 0.08 }, { opacity: 1, duration: 1, ease: 'sine.inOut' }, 0);
  }, { scope: stageRef, dependencies: [reducedMotion], revertOnUpdate: true });

  return (
    <div className="ambient-stage" ref={stageRef} aria-hidden="true">
      <div className="ambient-fibers">
        <div className="fiber-scene">
          <GhostFibers
            key={reducedMotion ? 'still' : 'animated'}
            glowColor={dark ? '#8b5cf6' : '#7845cc'}
            lineColor={dark ? '#e8dfff' : '#532999'}
            secondaryColor={dark ? '#57308a' : '#a481cf'}
            layers={3}
            twist={1.2}
            threads={compact ? 48 : 72}
            density={compact ? 0.65 : 1}
            fiberStyle="dots"
            depthOfField={0.55}
            hoverEffect="focus"
            intro={!reducedMotion}
            mouseInteraction={!paused && mode !== 'particles' && !compact}
            pulseOnClick={!paused && mode !== 'particles'}
            paused={paused || mode === 'particles'}
            lightMode={!dark}
            speed={0.35}
            flow={0.3}
            rotation={18}
            perspective={0.45}
            spread={0.9}
            scale={0.7}
            particles={compact ? 0.15 : 0.35}
            grain={0.025}
            glow={0.9}
            brightness={1.2}
            vignette={0.45}
            dpr={compact ? 1 : 1.5}
          />
        </div>
        <div className="hero-scrim" />
      </div>

      <div className="ambient-particles">
        <Suspense fallback={null}>
          <Particles
            key={reducedMotion ? 'still' : 'animated'}
            particleColors={dark ? ['#eee6ff', '#c5a6f4', '#8b5cf6'] : ['#7442b6', '#554565']}
            particleCount={compact ? 100 : 200}
            particleSpread={10}
            speed={0.1}
            particleBaseSize={85}
            glow={0.5}
            twinkle={0.3}
            depthOfField={0.3}
            moveParticlesOnHover={!compact && !paused && mode !== 'fibers'}
            hoverEffect="parallax"
            particleHoverFactor={0.6}
            clickBurst={false}
            intro={false}
            opacity={dark ? 0.8 : 0.55}
            lightMode={!dark}
            paused={paused || mode === 'fibers'}
            pixelRatio={compact ? 1 : 1.5}
          />
        </Suspense>
      </div>
    </div>
  );
}
