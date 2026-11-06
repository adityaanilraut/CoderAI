import { registerFlagDefinition } from '#/app/flag/flagRegistry';

export const FULL_COMPACTION_FLAG_ID = 'full_compaction';
// Upstream compaction is always assembled. Default on preserves that behavior;
// the host experiment only gates entry and never changes replay/state logic.
registerFlagDefinition({
  id: FULL_COMPACTION_FLAG_ID,
  title: 'Full compaction',
  description: 'Allow manual and automatic context compaction, including recovery from context overflow. Enabled by default; disabling it can expose the model context limit.',
  env: 'KIMI_CODE_EXPERIMENTAL_FULL_COMPACTION',
  default: true,
  surface: 'core',
});
