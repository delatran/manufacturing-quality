import { Children, isValidElement, useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Check, Copy } from 'lucide-react';
import type { Locale } from './i18n';

export function answerCodeText(value: ReactNode): string {
  if (typeof value === 'string' || typeof value === 'number') return String(value);
  if (isValidElement<{ children?: ReactNode }>(value)) return answerCodeText(value.props.children);
  return Children.toArray(value).map(answerCodeText).join('');
}

export default function AnswerCode({
  children,
  language,
}: {
  children?: ReactNode;
  language: Locale;
}) {
  const [copied, setCopied] = useState(false);
  const [failed, setFailed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const copyAttempt = useRef(0);
  useEffect(
    () => () => {
      copyAttempt.current += 1;
      clearTimeout(timer.current);
    },
    [],
  );
  const vi = language === 'vi';
  const code = Children.toArray(children).find(isValidElement);
  const syntax =
    code && isValidElement<{ className?: string }>(code)
      ? code.props.className?.match(/(?:^|\s)language-([\w+-]+)/)?.[1]
      : undefined;
  async function copy() {
    const attempt = ++copyAttempt.current;
    try {
      await navigator.clipboard.writeText(answerCodeText(children));
      if (attempt !== copyAttempt.current) return;
      setCopied(true);
      setFailed(false);
      clearTimeout(timer.current);
      timer.current = setTimeout(() => setCopied(false), 2000);
    } catch {
      if (attempt !== copyAttempt.current) return;
      setFailed(true);
      setCopied(false);
    }
  }
  return (
    <div className="answer-code" lang={language}>
      <div className="answer-code-toolbar">
        <span>{syntax || (vi ? 'Ví dụ' : 'Example')}</span>
        <button type="button" onClick={() => void copy()}>
          {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
          {copied ? (vi ? 'Đã sao chép' : 'Copied') : vi ? 'Sao chép mã' : 'Copy code'}
        </button>
      </div>
      <pre tabIndex={0} role="region" aria-label={vi ? 'Đoạn mã' : 'Code example'}>
        {children}
      </pre>
      <span className={failed ? 'answer-copy-error' : 'sr-only'} role="status">
        {failed
          ? vi
            ? 'Chưa sao chép được. Bạn có thể chọn và sao chép trực tiếp đoạn mã.'
            : 'Copy failed. You can select and copy the code directly.'
          : copied
            ? vi
              ? 'Đã sao chép mã.'
              : 'Code copied.'
            : ''}
      </span>
    </div>
  );
}
