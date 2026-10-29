import type { BearerTokenProvider } from '@coderai/oauth';

import { createOAuthCredentialProvider } from '#/credentials/credentials';
import type { LlmCredentialProvider } from '#/llm/requester/requester';

export function createKimiOAuthCredentialProvider(tokens: BearerTokenProvider): LlmCredentialProvider {
  return createOAuthCredentialProvider((options) => tokens.getAccessToken(options));
}
