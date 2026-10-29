import type { IFlagService } from '#/app/flag/flag';
import type { SkillDefinition } from '#/features/skill/catalog/types';

import { getBuiltinSkillContributions } from './registry';
import {
  SUB_SKILL_CONSOLIDATE,
  SUB_SKILL_PARENT,
  SUB_SKILL_REVIEW,
} from './sub-skill';

export const BUILTIN_SKILLS: readonly SkillDefinition[] = [
  SUB_SKILL_PARENT,
  SUB_SKILL_REVIEW,
  SUB_SKILL_CONSOLIDATE,
];

export function visibleBuiltinSkills(
  productSkillsEnabled: boolean,
  flags?: IFlagService,
): readonly SkillDefinition[] {
  const all = [...BUILTIN_SKILLS, ...getBuiltinSkillContributions()].filter((skill) => skill.productSpecific !== true);
  const visible = productSkillsEnabled
    ? all
    : all.filter((skill) => skill.productSpecific !== true);
  if (flags === undefined) return visible;
  return visible.filter(
    (skill) => skill.experimentalFlag === undefined || flags.enabled(skill.experimentalFlag),
  );
}

export {
  SUB_SKILL_CONSOLIDATE,
  SUB_SKILL_PARENT,
  SUB_SKILL_REVIEW,
};
