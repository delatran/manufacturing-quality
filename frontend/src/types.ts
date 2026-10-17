export type WorkspaceEvent = {
  id: string | number;
  session_id: string;
  run_id?: string;
  type: string;
  time: string;
  data: Record<string, unknown>;
};
export type UploadedFile = {
  id?: string;
  name: string;
  size?: number;
  sha256?: string;
};
export type Session = {
  id: string;
  events: WorkspaceEvent[];
  uploads?: UploadedFile[];
  runs?: {
    id: string;
    state: string;
    analysis_context_id?: string | null;
    language?: 'en' | 'vi';
  }[];
};
export type AppConfig = {
  mode: string;
  model: string;
  runtime?: string;
  agent_backend?: 'agents_api';
  unbounded_runs?: boolean;
  conversation_strategy?: 'managed_conversation';
  ai_available?: boolean;
  agent_available?: boolean;
  max_upload_mb?: number | null;
  max_cost_usd_per_run?: number | null;
  max_tool_calls?: number | null;
  max_hosted_tool_calls?: number | null;
  max_output_tokens?: number | null;
  max_turns?: number | null;
  run_timeout_seconds?: number | null;
  qa_max_retries?: number | null;
  dev_api_budget_usd?: number | null;
  dev_budget_remaining_usd?: number | null;
  limits?: Record<string, unknown>;
  [key: string]: unknown;
};
