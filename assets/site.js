(() => {
  const docMenu = document.querySelector('.doc-menu');
  if (docMenu) {
    const desktop = window.matchMedia('(min-width: 981px)');
    const adaptMenu = () => { docMenu.open = desktop.matches; };
    adaptMenu();
    desktop.addEventListener('change', adaptMenu);
  }
  const toggle = document.querySelector('.nav-toggle');
  const nav = document.querySelector('.site-nav');
  if (toggle && nav) {
    toggle.addEventListener('click', () => {
      const open = toggle.getAttribute('aria-expanded') === 'true';
      toggle.setAttribute('aria-expanded', String(!open));
      nav.dataset.open = String(!open);
    });
    nav.addEventListener('click', event => {
      if (!event.target.closest('a')) return;
      toggle.setAttribute('aria-expanded', 'false');
      nav.dataset.open = 'false';
    });
    document.addEventListener('keydown', event => {
      if (event.key !== 'Escape' || toggle.getAttribute('aria-expanded') !== 'true') return;
      toggle.setAttribute('aria-expanded', 'false');
      nav.dataset.open = 'false';
      toggle.focus();
    });
  }

  const toc = document.querySelector('[data-doc-toc]');
  const tocList = document.querySelector('[data-doc-toc-list]');
  const headings = [...document.querySelectorAll('.doc-content h2[id]')];
  if (toc && tocList && headings.length > 1) {
    const links = new Map();
    for (const heading of headings) {
      const link = document.createElement('a');
      link.href = `#${heading.id}`;
      link.textContent = heading.textContent;
      const item = document.createElement('li');
      item.append(link);
      tocList.append(item);
      links.set(heading.id, link);
    }
    toc.hidden = false;

    if ('IntersectionObserver' in window) {
      const observer = new IntersectionObserver(entries => {
        const visible = entries.find(entry => entry.isIntersecting);
        if (!visible) return;
        for (const link of links.values()) link.removeAttribute('aria-current');
        links.get(visible.target.id)?.setAttribute('aria-current', 'true');
      }, { rootMargin: '-15% 0px -75% 0px' });
      for (const heading of headings) observer.observe(heading);
    }
  }

  for (const table of document.querySelectorAll('.doc-content table')) {
    if (table.parentElement?.classList.contains('table-scroll')) continue;
    const region = document.createElement('div');
    region.className = 'table-scroll';
    region.setAttribute('role', 'region');
    region.setAttribute('tabindex', '0');
    region.setAttribute('aria-label', 'Scrollable data table');
    table.before(region);
    region.append(table);
  }

  if (navigator.clipboard) {
    document.querySelectorAll('.doc-content pre > code').forEach((code, index) => {
      code.id ||= `doc-code-${index}`;
      const wrapper = document.createElement('div');
      wrapper.className = 'code-example';
      code.parentElement.before(wrapper);
      wrapper.append(code.parentElement);
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'copy-button';
      button.dataset.copy = code.id;
      button.textContent = 'Copy';
      button.setAttribute('aria-label', 'Copy code');
      button.setAttribute('aria-live', 'polite');
      wrapper.prepend(button);
    });
  }

  for (const button of document.querySelectorAll('[data-copy]')) {
    const label = button.textContent;
    let resetLabel;
    button.addEventListener('click', async () => {
      const target = document.getElementById(button.dataset.copy);
      if (!target) return;
      window.clearTimeout(resetLabel);
      try {
        await navigator.clipboard.writeText(target.textContent.trim());
        button.textContent = 'Copied';
      } catch {
        const range = document.createRange();
        range.selectNodeContents(target);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        button.textContent = 'Copy selected text';
      }
      resetLabel = window.setTimeout(() => { button.textContent = label; }, 1400);
    });
  }
})();
