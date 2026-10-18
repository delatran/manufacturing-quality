import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Expand, Minimize2, X } from 'lucide-react';
import type { Locale } from './i18n';

export default function AnswerChart({
  src,
  alt,
  language,
}: {
  src: string;
  alt: string;
  language: Locale;
}) {
  const [open, setOpen] = useState(false);
  const [actualSize, setActualSize] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const closeButton = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const vi = language === 'vi';
  useEffect(() => {
    const element = dialog.current;
    const returnFocus = trigger.current;
    if (!open || !element) return;
    if (!element.open) element.showModal();
    closeButton.current?.focus({ preventScroll: true });
    return () => {
      if (element.open) element.close();
      if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true });
    };
  }, [open]);
  const portalRoot =
    trigger.current?.closest('.agent-workspace') ||
    (typeof document === 'undefined' ? null : document.body);
  return (
    <span className="answer-chart" lang={language}>
      <img src={src} alt={alt} loading="lazy" />
      <button
        type="button"
        className="answer-chart-open"
        ref={trigger}
        aria-haspopup="dialog"
        aria-expanded={open}
        onClick={() => {
          setActualSize(false);
          setOpen(true);
        }}
      >
        <Expand size={15} aria-hidden="true" />
        {vi ? 'Xem biểu đồ lớn' : 'Expand chart'}
      </button>
      {open &&
        portalRoot &&
        createPortal(
          <dialog
            className="answer-chart-dialog"
            ref={dialog}
            aria-labelledby={titleId}
            lang={language}
            onCancel={(event) => {
              event.preventDefault();
              setOpen(false);
            }}
            onClose={() => setOpen(false)}
          >
            <div className="answer-chart-header">
              <h2 id={titleId}>{alt}</h2>
              <button type="button" onClick={() => setActualSize((value) => !value)}>
                <Minimize2 size={16} aria-hidden="true" />
                {actualSize
                  ? vi
                    ? 'Vừa màn hình'
                    : 'Fit to view'
                  : vi
                    ? 'Kích thước gốc'
                    : 'Actual size'}
              </button>
              <button
                type="button"
                ref={closeButton}
                onClick={() => setOpen(false)}
                aria-label={vi ? 'Đóng biểu đồ' : 'Close chart'}
              >
                <X size={20} aria-hidden="true" />
              </button>
            </div>
            <div
              className={'answer-chart-image' + (actualSize ? ' is-actual-size' : '')}
              tabIndex={0}
              role="region"
              aria-label={alt}
            >
              <img src={src} alt={alt} />
            </div>
          </dialog>,
          portalRoot,
        )}
    </span>
  );
}
