import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { ArrowDownToLine, Info } from 'lucide-react';
import { useDashboardLanguage } from './dashboard-language';
import type { IconComponent } from './dashboard-display-types';

export function Section({
  id,
  kicker,
  title,
  caption,
  children,
}: {
  id: string;
  kicker: string;
  title: string;
  caption?: string;
  children: ReactNode;
}) {
  return (
    <section className="dashboard-section" id={id} aria-labelledby={`${id}-title`}>
      <header className="dashboard-section-head">
        <span className="section-kicker">{kicker}</span>
        <h2 id={`${id}-title`}>{title}</h2>
        {caption && <p className="dashboard-section-caption">{caption}</p>}
      </header>
      {children}
    </section>
  );
}

export function SectionNav({
  items,
  label,
}: {
  items: { id: string; label: string; icon: IconComponent }[];
  label: string;
}) {
  const [active, setActive] = useState(items[0]?.id);
  const ids = items.map((item) => item.id).join(' ');
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(
      (entries) => {
        const visible = entries
          .filter((entry) => entry.isIntersecting)
          .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top);
        if (visible[0]) setActive(visible[0].target.id);
      },
      { rootMargin: '-170px 0px -55% 0px' },
    );
    for (const id of ids.split(' ')) {
      const element = document.getElementById(id);
      if (element) observer.observe(element);
    }
    return () => observer.disconnect();
  }, [ids]);
  return (
    <nav className="dashboard-nav" aria-label={label}>
      <label className="mobile-section-select">
        <span>{label}</span>
        <select
          value={active ?? items[0]?.id}
          onChange={(event) => {
            const id = event.target.value;
            setActive(id);
            const section = document.getElementById(id);
            section?.scrollIntoView({ block: 'start' });
            const heading = document.getElementById(`${id}-title`);
            heading?.setAttribute('tabindex', '-1');
            heading?.focus({ preventScroll: true });
          }}
        >
          {items.map((item) => (
            <option key={item.id} value={item.id}>
              {item.label}
            </option>
          ))}
        </select>
      </label>
      <ul>
        {items.map(({ id, label: text, icon: Icon }) => (
          <li key={id}>
            <a
              href={`#${id}`}
              className={active === id ? 'active' : undefined}
              aria-current={active === id ? 'location' : undefined}
              onClick={() => setActive(id)}
            >
              <Icon size={16} aria-hidden="true" />
              <span>{text}</span>
            </a>
          </li>
        ))}
      </ul>
    </nav>
  );
}

export function Panel({
  kicker,
  title,
  children,
  className = '',
}: {
  kicker: string;
  title: string;
  children: ReactNode;
  className?: string;
}) {
  const { tr } = useDashboardLanguage();

  return (
    <section className={`dashboard-panel ${className}`}>
      <span className="section-kicker">{tr(kicker)}</span>
      <h3>{tr(title)}</h3>
      {children}
    </section>
  );
}

export function InfoTip({ text, label }: { text: string; label: string }) {
  return (
    <span
      className="info-tip"
      tabIndex={0}
      role="note"
      aria-label={`${label}: ${text}`}
      data-tip={text}
    >
      <Info size={14} aria-hidden="true" />
    </span>
  );
}

export function ShareCard({
  icon: Icon,
  title,
  description,
  href,
  action,
  note,
}: {
  icon: IconComponent;
  title: string;
  description: string;
  href: string;
  action: string;
  note?: string;
}) {
  return (
    <article className="share-card">
      <span className="share-icon">
        <Icon size={18} aria-hidden="true" />
      </span>
      <h3>{title}</h3>
      <p>{description}</p>
      {note && <small className="workbook-note">{note}</small>}
      <a className="secondary-button" href={href}>
        <ArrowDownToLine size={15} aria-hidden="true" />
        {action}
      </a>
    </article>
  );
}

export function QualityStat({ label, value }: { label: string; value: number }) {
  const { tr, number } = useDashboardLanguage();

  return (
    <div>
      <strong>{number(value)}</strong>
      <span>{tr(label)}</span>
    </div>
  );
}
