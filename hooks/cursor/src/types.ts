/** Cursor hook event names registered in ~/.cursor/hooks.json. */
export const EVENT_HOOKS = [
  "beforeSubmitPrompt",
  "afterAgentResponse",
  "afterAgentThought",
  "beforeShellExecution",
  "afterShellExecution",
  "beforeMCPExecution",
  "afterMCPExecution",
  "beforeReadFile",
  "afterFileEdit",
] as const;

export type EventHookName = (typeof EVENT_HOOKS)[number];

/** All 11 hook event names (9 events + stop + sessionStart). */
export const ALL_HOOK_EVENTS = [...EVENT_HOOKS, "stop", "sessionStart"] as const;

/** Payload received via stdin from Cursor. */
export interface CursorHookPayload {
  hook_event_name: string;
  conversation_id?: string;
  generation_id?: string;
  model?: string;
  workspace_roots?: string[];
  user_email?: string;
  // beforeSubmitPrompt
  prompt?: string;
  attachments?: unknown;
  // afterAgentResponse / afterAgentThought
  text?: string;
  duration_ms?: number;
  // beforeShellExecution / afterShellExecution
  command?: string;
  cwd?: string;
  output?: string;
  duration?: number;
  // beforeMCPExecution / afterMCPExecution
  tool_name?: string;
  tool_input?: unknown;
  url?: string;
  result_json?: string;
  // beforeReadFile
  file_path?: string;
  content?: string;
  // afterFileEdit
  edits?: unknown;
  // stop
  status?: string;
  loop_count?: number;
  [key: string]: unknown;
}

/** A single JSONL record appended to the per-conversation state file. */
export interface StateRecord {
  hook_event_name: string;
  generation_id?: string;
  timestamp: string;
  conversation_id?: string;
  model?: string;
  user_email?: string;
  [key: string]: unknown;
}

/** A grouped set of events forming one agent turn. */
export interface Turn {
  conversationId: string;
  events: StateRecord[];
  userInput?: string;
  finalOutput?: string;
  model?: string;
  startTime: string;
  endTime: string;
  userEmail?: string;
  cursorStatus?: string;
}
