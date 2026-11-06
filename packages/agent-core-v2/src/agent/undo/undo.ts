import { createDecorator, type ServiceIdentifier } from '#/_base/di/instantiation';

export interface UndoAvailability {
  readonly maxTurns: number;
  readonly stoppedAtCompaction: boolean;
}

export interface IAgentConversationUndoService {
  readonly _serviceBrand: undefined;

  availability(): UndoAvailability;
  undo(turns: number, options?: { readonly revertFiles?: boolean }): Promise<number>;
}

export const IAgentConversationUndoService: ServiceIdentifier<IAgentConversationUndoService> =
  createDecorator<IAgentConversationUndoService>('agentConversationUndoService');
