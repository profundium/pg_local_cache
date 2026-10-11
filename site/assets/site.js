(() => {
  const trackEvent = (name, parameters) => {
    try {
      if (typeof window.gtag === 'function') window.gtag('event', name, parameters);
    } catch {
      // Analytics failures must not affect site behavior.
    }
  };

  const codeKind = text => {
    try {
      const source = String(text);
      const installCommands = [
        /(?:^|[\n;&|])[ \t]*(?:\$[ \t]*)?(?:(?:sudo|doas)[ \t]+)*(?:apt-get|apt|dnf|yum)\b[^\n;&|]*\binstall\b/im,
        /(?:^|[\n;&|])[ \t]*(?:\$[ \t]*)?(?:(?:sudo|doas)[ \t]+)*pgxn\b[^\n;&|]*\binstall\b/im,
        /(?:^|[\n;&|])[ \t]*(?:\$[ \t]*)?(?:(?:sudo|doas)[ \t]+)*(?:g?make)[ \t]+install\b/im,
        /(?:^|[\n;&|])[ \t]*(?:\$[ \t]*)?(?:(?:sudo|doas)[ \t]+)*nix(?:-env)?\b/im,
        /(?:^|[\n;&|])[ \t]*(?:\$[ \t]*)?(?:(?:sudo|doas)[ \t]+)*pig\b[^\n;&|]*\binstall\b/im,
        /(?:^|[\n;&|])[ \t]*(?:\$[ \t]*)?(?:(?:sudo|doas)[ \t]+)*(?:rpm|dpkg)[ \t]+-i\b/im,
      ];
      if (installCommands.some(pattern => pattern.test(source))) return 'install';
      if (/\b(?:CREATE\s+EXTENSION|SELECT\b|ALTER\s+SYSTEM|attach_table\b)/i.test(source)) return 'sql';
      if (/\bredis-cli\b|\bMGET\b|\bRESP2?\b|\b(?:redis|valkey)\.NewClient\b|\bMGet\s*\(|\b(?:client|conn|connection)\.(?:Do|Send|Receive|SendCommand)\s*\(/i.test(source)) return 'resp';
    } catch {
      return 'other';
    }
    return 'other';
  };

  document.addEventListener('click', event => {
    try {
      const anchor = event.target instanceof Element && event.target.closest('a[href]');
      if (!anchor) return;
      const href = anchor.getAttribute('href');
      if (!href) return;
      const url = new URL(href, location);
      if (url.origin === location.origin) return;
      const hostname = url.hostname.toLowerCase();
      const pathname = url.pathname;
      const linkUrl = `${url.origin}${pathname}`;
      const isProjectRepository = hostname === 'github.com' && /^\/profundium\/pg_local_cache(?:\/|$)/i.test(pathname);
      const isProjectRelease = isProjectRepository && /^\/profundium\/pg_local_cache\/releases(?:\/|$)/i.test(pathname);
      const isPackageRepositoryHost = /^(?:repo|packages?|apt|yum|dnf|rpm|deb)\./i.test(hostname);
      const isPackageRepositoryPath = /(?:^|\/)(?:packages?|repos?|repositories?|apt|yum|dnf|rpm|deb)(?:\/|$)/i.test(pathname);
      const isProjectPackagePath = /(?:^|\/)(?:pg_local_cache|pglc)(?:[-/.]|$)/i.test(pathname);
      const isPackageArtifact = /\.(?:deb|rpm|pkg|tar\.gz)$/i.test(pathname);
      const isPigstyRepository = /^repo\.pigsty\.(?:io|cc)$/i.test(hostname) &&
        (/^\/(?:apt|yum)(?:\/|$)/i.test(pathname) || isProjectPackagePath || isPackageArtifact);
      const isPigstyProjectPage = hostname === 'ext.pigsty.io' &&
        /^\/e\/pg_local_cache(?:\/|$)/i.test(pathname);

      if (isProjectRelease) {
        trackEvent('download_click', { link_url: linkUrl, destination: 'github_release', transport_type: 'beacon' });
        return;
      }
      if (isProjectRepository) {
        trackEvent('repo_click', { link_url: linkUrl, transport_type: 'beacon' });
        return;
      }

      let destination = null;
      if (hostname === 'pgxn.org' && /^\/dist\/pg_local_cache(?:\/|$)/i.test(pathname)) {
        destination = 'pgxn';
      } else if (isPigstyRepository || isPigstyProjectPage) {
        destination = 'pigsty';
      } else if (
        (isPackageRepositoryHost && (isPackageRepositoryPath || isProjectPackagePath || isPackageArtifact)) ||
        (isProjectPackagePath && (isPackageRepositoryPath || isPackageArtifact))
      ) {
        destination = 'other';
      }

      if (destination) {
        trackEvent('download_click', { link_url: linkUrl, destination, transport_type: 'beacon' });
      }
    } catch {
      // Malformed links or analytics errors must not interrupt navigation.
    }
  });

  const labels = document.body.dataset;
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
    region.setAttribute('aria-label', labels.scrollTable);
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
      button.textContent = labels.copyLabel;
      button.setAttribute('aria-label', labels.copyCode);
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
      let copyText = '';
      let copyResult;
      try {
        copyText = target.textContent.trim();
        await navigator.clipboard.writeText(copyText);
        button.textContent = labels.copied;
        copyResult = 'copied';
      } catch {
        const range = document.createRange();
        range.selectNodeContents(target);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        button.textContent = labels.copySelected;
        copyResult = 'selected';
      }
      trackEvent('copy_code', { code_kind: codeKind(copyText), copy_result: copyResult });
      resetLabel = window.setTimeout(() => { button.textContent = label; }, 1400);
    });
  }
})();
