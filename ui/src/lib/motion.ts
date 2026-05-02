/**
 * Reduced-motion controls.
 *
 * Honors three opt-outs (in order):
 *   - CODERAI_NO_ANIMATION=1
 *   - NO_MOTION=1            (informal cross-tool convention)
 *   - TERM=dumb              (most "dumb" terminals can't render spinner frames cleanly)
 *
 * Resolved once at module load — env changes during a session don't take
 * effect, which matches every other terminal-UI in the wild.
 */

function resolve(): boolean {
  const env = process.env;
  if (env.CODERAI_NO_ANIMATION === "1") return false;
  if (env.NO_MOTION === "1") return false;
  if (env.TERM === "dumb") return false;
  return true;
}

export const motionEnabled: boolean = resolve();
