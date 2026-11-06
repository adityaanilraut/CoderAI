import type { PromptSkillActivation } from '@coderai/sdk';
/** Upstream token boundaries: whitespace before slash, no paths or URLs, deduplicate in input order. */
export function inlineSkillActivations(text: string, skills: ReadonlyMap<string, string>): PromptSkillActivation[] {
  const seen = new Set<string>();
  const result: PromptSkillActivation[] = [];
  for (const token of text.matchAll(/\s\/(\S+)/g)) {
    const command = token[1]!;
    if (command.includes('/')) continue;
    const name = skills.get(command) ?? skills.get(`skill:${command}`) ?? (command.startsWith('skill:') ? skills.get(command.slice(6)) : undefined);
    if (!name || seen.has(name)) continue;
    seen.add(name); result.push({ name });
  }
  return result;
}
