import type { Filters } from './dashboard-types';

export type WorkspacePanel = 'dashboard' | 'records' | 'evidence' | 'investigation' | 'brief';
export type UiCommand = {
  command_id: string;
  action: 'open' | 'apply_scope' | 'undo';
  panel: WorkspacePanel;
  context_id: string;
  expected_context_id: string;
  previous_context_id?: string;
  run_context_id?: string;
  filters?: Filters;
  reversible: boolean;
};

const panels = new Set<WorkspacePanel>(['dashboard', 'records', 'evidence']);

export function isVisibleWorkspacePanel(panel: WorkspacePanel): boolean {
  return panels.has(panel);
}
export function parseUiCommand(value: Record<string, unknown>): UiCommand | undefined {
  if (
    typeof value.command_id !== 'string' ||
    !value.command_id ||
    !['open', 'apply_scope', 'undo'].includes(String(value.action)) ||
    !isVisibleWorkspacePanel(value.panel as WorkspacePanel) ||
    typeof value.context_id !== 'string' ||
    !value.context_id.trim() ||
    typeof value.expected_context_id !== 'string' ||
    !value.expected_context_id.trim() ||
    typeof value.reversible !== 'boolean'
  )
    return;
  return value as UiCommand;
}

export function commandMatchesContext(command: UiCommand, contextId?: string): boolean {
  return Boolean(contextId && command.expected_context_id === contextId);
}

export function applyPriorityFilters(applied: Filters, focus: Partial<Filters>): Filters {
  return { ...applied, ...focus };
}
