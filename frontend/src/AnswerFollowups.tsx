import { memo, useMemo } from 'react';
import { ArrowRight } from 'lucide-react';
import type { Locale } from './i18n';
import type { WorkspaceEvent } from './types';
import { answerFollowups, answerSourceQuestion } from './answer-followups';

export type AnswerFollowupsProps = {
  report: WorkspaceEvent;
  question?: WorkspaceEvent;
  language: Locale;
  eligible?: boolean;
  disabled?: boolean;
  onFollowUp?: (sourceReport: WorkspaceEvent, prompt: string, language: Locale) => void;
};

const AnswerFollowups = memo(function AnswerFollowups({
  report,
  question,
  language,
  eligible = false,
  disabled = false,
  onFollowUp,
}: AnswerFollowupsProps) {
  const choices = useMemo(
    () => (eligible === true ? answerFollowups(report, question, language) : []),
    [eligible, report, question, language],
  );
  if (!choices.length || typeof onFollowUp !== 'function') return null;
  const sourceQuestion = answerSourceQuestion(report, question);
  const isVi = language === 'vi';
  return (
    <section
      className="answer-followups"
      lang={language}
      aria-label={isVi ? 'Hỏi tiếp về câu trả lời này' : 'Follow up on this answer'}
    >
      {sourceQuestion && (
        <p className="answer-followup-source">
          <span>{isVi ? 'Từ câu hỏi:' : 'From the question:'}</span> {sourceQuestion}
        </p>
      )}
      <div className="answer-followup-actions">
        {choices.map((choice) => (
          <button
            key={choice.id}
            type="button"
            disabled={disabled}
            title={
              isVi
                ? 'Soạn câu hỏi để xem lại trước khi gửi'
                : 'Prepare a question to review before sending'
            }
            onClick={() => {
              if (!disabled) onFollowUp(report, choice.prompt, language);
            }}
          >
            {choice.label}
            <ArrowRight size={15} aria-hidden="true" />
          </button>
        ))}
      </div>
    </section>
  );
});

export default AnswerFollowups;
