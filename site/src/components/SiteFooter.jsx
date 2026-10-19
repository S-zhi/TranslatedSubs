import { ArrowUpIcon, ArrowUpRightIcon, GithubLogoIcon } from '@phosphor-icons/react';
import { FOOTER_GROUPS, REPOSITORY_URL } from '../content/site-links.js';
import './SiteFooter.css';

export default function SiteFooter() {
  return (
    <footer className="site-footer" id="resources">
      <div className="site-footer__inner">
        <div className="site-footer__top">
          <div className="site-footer__brand">
            <a className="brand" href="#overview" aria-label="TranslatedSubs 首页">
              <img src={`${import.meta.env.BASE_URL}brand.svg`} width="34" height="34" alt="" />
              <span>Translated<span className="brand__ending">Subs</span></span>
            </a>
            <p>让好内容，跨越语言。</p>
          </div>

          <nav className="site-footer__nav" aria-label="页脚导航">
            {FOOTER_GROUPS.map(group => (
              <div className="site-footer__group" key={group.title}>
                <h3>{group.title}</h3>
                <ul>
                  {group.links.map(link => {
                    const external = link.href.startsWith('https://');
                    return (
                      <li key={link.href}>
                        <a href={link.href} target={external ? '_blank' : undefined} rel={external ? 'noreferrer' : undefined}>
                          {link.label}
                          {external && <ArrowUpRightIcon size={12} aria-hidden="true" />}
                        </a>
                      </li>
                    );
                  })}
                </ul>
              </div>
            ))}
          </nav>
        </div>

        <div className="site-footer__bottom">
          <a className="site-footer__github" href={REPOSITORY_URL} target="_blank" rel="noreferrer">
            <GithubLogoIcon size={20} aria-hidden="true" /> GitHub
          </a>
          <p>TranslatedSubs <span>开源视频字幕工作台</span></p>
          <a className="site-footer__back" href="#overview">返回顶部 <ArrowUpIcon size={15} aria-hidden="true" /></a>
        </div>
      </div>
    </footer>
  );
}
