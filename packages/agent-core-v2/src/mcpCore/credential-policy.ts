import type { McpServerConfig } from './config-schema';

// Internal origin metadata is carried through config copies and never parsed from project JSON.
export const mcpCredentialOrigin = Symbol('mcpCredentialOrigin');

export interface McpCredentialGrant {
  readonly server: string;
  readonly origin: string;
  readonly target: string;
  readonly variables: readonly string[];
}

export function tagMcpCredentialOrigin(config: McpServerConfig, origin: string): McpServerConfig {
  const tagged = { ...config, [mcpCredentialOrigin]: origin };
  return tagged;
}

export function getMcpCredentialOrigin(config: McpServerConfig): string | undefined {
  return (config as McpServerConfig & { [mcpCredentialOrigin]?: string })[mcpCredentialOrigin];
}

export function mcpGrantedVariables(
  config: McpServerConfig,
  server: string,
  grants: readonly McpCredentialGrant[] = [],
): ReadonlySet<string> | undefined {
  const origin = getMcpCredentialOrigin(config);
  if (origin === undefined) return undefined;
  const target = config.transport === 'stdio' ? config.command : config.url;
  return new Set(grants.filter(grant => grant.origin === origin && grant.server === server &&
    grant.target === target).flatMap(grant => grant.variables));
}
