// 作品页交互说明：只演示固定流程，不调用真实 Reelwave AI。
(() => {
  const dialog = document.querySelector('[data-demo-dialog]');
  document.querySelectorAll('[data-open-demo]').forEach((button) => {
    button.addEventListener('click', () => dialog?.showModal());
  });
  dialog?.addEventListener('click', (event) => {
    if (event.target === dialog) dialog.close();
  });

  const heroNext = document.querySelector('[data-hero-next]');
  const heroCooking = document.querySelector('[data-hero-cooking]');
  const heroInspector = document.querySelector('[data-hero-inspector]');
  const heroStatus = document.querySelector('[data-hero-status]');
  const heroApply = document.querySelector('[data-hero-apply]');
  const heroDismiss = document.querySelector('[data-hero-dismiss]');
  const heroDecision = document.querySelector('[data-hero-decision]');
  let heroStep = 0;

  const resetHero = () => {
    heroStep = 0;
    heroCooking?.classList.remove('is-moving');
    heroInspector?.classList.remove('is-shown');
    if (heroStatus) heroStatus.textContent = '移动素材，改变故事。';
    if (heroDecision) heroDecision.textContent = '每一次建议都需要创作者确认。';
    if (heroNext) heroNext.textContent = '播放交互演示';
  };

  heroNext?.addEventListener('click', () => {
    heroStep += 1;
    if (heroStep === 1) {
      heroCooking?.classList.add('is-moving');
      if (heroStatus) heroStatus.textContent = '「做饭」从下班后的生活移到了开场。';
      heroNext.textContent = '查看 AI 建议';
    } else if (heroStep === 2) {
      heroInspector?.classList.add('is-shown');
      if (heroStatus) heroStatus.textContent = 'Reelwave 理解了这次编辑，并提出一个局部修改建议。';
      heroNext.textContent = '重置';
    } else {
      resetHero();
    }
  });
  heroApply?.addEventListener('click', () => {
    heroInspector?.classList.add('is-applied');
    if (heroStatus) heroStatus.textContent = '建议已应用。创作者始终掌控全局。';
    if (heroDecision) heroDecision.textContent = '已应用到 Beat 01 · 保留可撤回。';
  });
  heroDismiss?.addEventListener('click', () => {
    heroInspector?.classList.remove('is-applied');
    if (heroStatus) heroStatus.textContent = '建议已忽略。素材移动仍然由创作者自己决定其含义。';
    if (heroDecision) heroDecision.textContent = '已保留原故事结构。';
  });

  const dragDemo = document.querySelector('[data-drag-demo]');
  const cookingClip = dragDemo?.querySelector('[data-cooking-clip]');
  const targetBeat = dragDemo?.querySelector('[data-target-beat]');
  const storyPanel = dragDemo?.querySelector('[data-story-panel]');
  const result = dragDemo?.querySelector('[data-drag-result]');
  const apply = dragDemo?.querySelector('[data-apply-change]');
  const dismiss = dragDemo?.querySelector('[data-dismiss-change]');

  const showStoryChange = () => {
    cookingClip?.classList.add('is-moved');
    targetBeat?.classList.add('is-target');
    storyPanel?.classList.add('is-active');
    if (result) result.textContent = '编辑动作已被理解：这条素材现在支撑着开场部分。';
  };
  const resetStoryChange = (message) => {
    cookingClip?.classList.remove('is-moved');
    targetBeat?.classList.remove('is-target');
    storyPanel?.classList.remove('is-active');
    if (result) result.textContent = message;
  };

  cookingClip?.addEventListener('click', showStoryChange);
  cookingClip?.addEventListener('dragstart', (event) => event.dataTransfer?.setData('text/plain', 'cooking'));
  targetBeat?.addEventListener('dragover', (event) => {
    event.preventDefault();
    targetBeat.classList.add('is-target');
  });
  targetBeat?.addEventListener('dragleave', () => {
    if (!cookingClip?.classList.contains('is-moved')) targetBeat.classList.remove('is-target');
  });
  targetBeat?.addEventListener('drop', (event) => {
    event.preventDefault();
    showStoryChange();
  });
  apply?.addEventListener('click', () => {
    if (result) result.textContent = '已应用：意图和文案完成局部更新，AI 并没有重写整个故事。';
  });
  dismiss?.addEventListener('click', () => resetStoryChange('已忽略：建议已撤销。这次移动素材的含义仍由创作者自己决定。'));

  const progress = document.querySelector('[data-reading-progress]');
  const navLinks = [...document.querySelectorAll('.site-header nav a')];
  const navTargets = navLinks.map((link) => ({ link, target: document.querySelector(link.getAttribute('href')) })).filter(({ target }) => target);

  const updateReadingProgress = () => {
    const scrollableHeight = document.documentElement.scrollHeight - window.innerHeight;
    const percentage = scrollableHeight > 0 ? Math.min(100, (window.scrollY / scrollableHeight) * 100) : 0;
    if (progress) progress.style.transform = `scaleX(${percentage / 100})`;
  };

  if (progress) {
    updateReadingProgress();
    window.addEventListener('scroll', updateReadingProgress, { passive: true });
    window.addEventListener('resize', updateReadingProgress);
  }

  if ('IntersectionObserver' in window) {
    const navObserver = new IntersectionObserver((entries) => {
      const visible = entries.filter((entry) => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
      if (!visible) return;
      navLinks.forEach((link) => link.removeAttribute('aria-current'));
      const current = navTargets.find(({ target }) => target === visible.target)?.link;
      current?.setAttribute('aria-current', 'page');
    }, { rootMargin: '-22% 0px -65% 0px', threshold: [0.05, 0.4] });
    navTargets.forEach(({ target }) => navObserver.observe(target));
  }

  const revealItems = document.querySelectorAll('.section-intro, .evolution-step, .realization, .story-structure article, .lesson-list article');
  if ('IntersectionObserver' in window && !window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
    document.documentElement.classList.add('has-reveal');
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (entry.isIntersecting) {
          entry.target.classList.add('is-visible');
          observer.unobserve(entry.target);
        }
      });
    }, { threshold: 0.12 });
    revealItems.forEach((item) => observer.observe(item));
  }
})();
