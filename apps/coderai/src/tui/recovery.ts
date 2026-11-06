import { singleLine } from './status.js';

export interface RecoveryNotice { message: string; recovery: string; cancelled: boolean }

export function recoveryNotice(error: unknown): RecoveryNotice {
  const record = error && typeof error === 'object' ? error as Record<string, unknown> : undefined;
  const code = typeof record?.['code'] === 'string' ? record['code'] : '';
  const raw = typeof record?.['message'] === 'string' ? record['message'] : String(error);
  let message = singleLine(raw);
  try {
    const parsed = JSON.parse(raw);
    if (typeof parsed?.error?.message === 'string') message = singleLine(parsed.error.message);
  } catch { /* Most provider messages are already plain text. */ }
  const cancelled = record?.['name'] === 'AbortError' || message === 'Aborted' || message.endsWith(': Aborted');
  let recovery: string;
  if (cancelled) recovery = '';
  else if (code === 'provider.auth_error' || /\b(401|403|invalid api.?key|authentication|invalid.*credential)\b/i.test(message)) {
    recovery = 'Check your provider API key with coderai --setup, then reopen with coderai --continue.';
  } else if (code === 'model.not_found' || code === 'provider.not_found' || /model.*(not found|does not exist)/i.test(message)) {
    recovery = 'Use /model to choose a configured model, or run coderai --setup to add one.';
  } else if (code === 'context.overflow' || /context.*(limit|exceed|overflow|too long)/i.test(message)) {
    recovery = 'Use /compact to reduce context, /model for a larger model, or /new to start fresh.';
  } else if (code === 'provider.rate_limit' || /\b429\b|rate.?limit/i.test(message)) {
    recovery = 'Wait for the provider limit to reset, then use ↑ to recall your prompt and try again.';
  } else if (code === 'provider.connection_error' || /\b(ECONNREFUSED|ENOTFOUND|ETIMEDOUT|fetch failed)\b/i.test(message)) {
    recovery = 'Check your connection and provider endpoint, then use ↑ to recall your prompt and try again.';
  } else if (code === 'provider.filtered') {
    recovery = 'Revise the prompt or choose another model with /model.';
  } else if (record?.['retryable'] === true || code === 'provider.overloaded') {
    recovery = 'Use ↑ to recall your prompt and try again, or choose another model with /model.';
  } else {
    recovery = 'Review the message and try again. Use /export-debug to save diagnostics if it persists.';
  }
  return { message: cancelled ? 'Cancelled.' : message || 'An unexpected error occurred.', recovery, cancelled };
}
