import { createDecorator } from "#/_base/di/instantiation";

export type PlanData = null | {
  readonly id: string;
  readonly content: string;
  readonly path: string;
};

export type PlanFilePath = string | null;

export interface IAgentPlanService {
  readonly _serviceBrand: undefined;
  readonly isActive: boolean;
  readonly hasEffectivePlan: boolean;

  enter(id?: string, createFile?: boolean): Promise<void>;
  cancel(id?: string): void;
  clear(): Promise<void>;
  exit(id?: string): void;
  recordRevision(): Promise<void>;
  status(): Promise<PlanData>;
}

export const IAgentPlanService =
  createDecorator<IAgentPlanService>('agentPlanService');
