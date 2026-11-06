import type { ToolInputDisplay } from '@coderai/agent-core-v2/tool/toolInputDisplay';
import type { ContentPart } from '@coderai/kosong';

export type {
  ApprovalDecision,
  ApprovalResponse,
} from '@coderai/agent-core-v2/agent/interaction/approval';
export type {
  QuestionAnswerMethod,
  QuestionAnswers,
  QuestionItem,
  QuestionOption,
  QuestionRequest,
  QuestionResponse,
  QuestionResult,
} from '@coderai/agent-core-v2/agent/interaction/question';

export type ApprovalScope = 'session';

export interface ApprovalRequest {
  readonly turnId?: number | undefined;
  readonly toolCallId: string;
  readonly toolName: string;
  readonly action: string;
  readonly display: ToolInputDisplay;
}

export type { UserToolRegistration } from '@coderai/agent-core-v2/agent/userTool/userTool';

/** Execute a registered SDK tool; errors become error tool results. */
export type UserToolHandler = (args: unknown, request: ToolCallRequest) => ToolCallResponse | Promise<ToolCallResponse>;

export interface ToolCallRequest {
  readonly sessionId: string;
  readonly agentId: string;
  readonly name: string;
  readonly turnId?: number | undefined;
  readonly toolCallId: string;
  readonly args: unknown;
}

export interface ToolCallResponse {
  readonly output: string | ContentPart[];
  readonly isError?: boolean | undefined;
}
