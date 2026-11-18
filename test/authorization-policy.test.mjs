import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm } from 'node:fs/promises';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { engineBundlePlugins } from '../scripts/bundle-options.mjs';

const dir = await mkdtemp(resolve('node_modules/.authorization-'));
const entry = join(dir, 'policy.mjs');
let policy;
try {
  await build({
    stdin: { resolveDir: process.cwd(), contents: `
      export { resolveToolPath } from './packages/agent-core-v2/src/tool/path-access.ts';
      export { matchesToolPathRule } from './packages/agent-core-v2/src/tool/rule-match.ts';
      export { mcpGrantedVariables, tagMcpCredentialOrigin } from './packages/agent-core-v2/src/mcpCore/credential-policy.ts';
      export { mergeStdioEnv } from './packages/agent-core-v2/src/mcpCore/client-stdio.ts';
      export { AgentPermissionPolicyService } from './packages/agent-core-v2/src/agent/permissionPolicy/permissionPolicyService.ts';
      export { AgentPlanService } from './packages/agent-core-v2/src/features/plan/planService.ts';
    ` },
    outfile: entry, bundle: true, platform: 'node', format: 'esm', target: 'node24',
    tsconfigRaw: { compilerOptions: { experimentalDecorators: true } },
    plugins: engineBundlePlugins(),
  });
  policy = await import(pathToFileURL(entry).href);
} finally {
  await rm(dir, { recursive: true, force: true });
}

function fakeFs(entries) {
  return {
    async realpath(path) {
      if (entries.has(path)) return entries.get(path);
      throw Object.assign(new Error('Path missing'), { code: 'ENOENT' });
    },
    async lstat() { throw Object.assign(new Error('Path missing'), { code: 'ENOENT' }); },
  };
}

const workspace = { workspaceDir: '/project', additionalDirs: [] };
const env = { pathClass: 'posix', homeDir: '/home/user' };
const credentials = { homeDir: '/app', configPath: '/app/config.toml' };
const options = { workspace, env, operation: 'write' };

test('resolved tool paths retain an alias for restrictive rules and validate targets at execution', async () => {
  const entries = new Map([
    ['/', '/'], ['/app', '/app'], ['/project', '/project'], ['/project/alias.txt', '/project/actual.txt'],
    ['/project/actual.txt', '/project/actual.txt'],
  ]);
  const fs = fakeFs(entries);
  const target = await policy.resolveToolPath(fs, 'alias.txt', options, credentials);
  const ruleOptions = { cwd: workspace.workspaceDir, pathClass: env.pathClass };
  assert.equal(target.path, '/project/actual.txt');
  assert.equal(target.requestedPath, '/project/alias.txt');
  assert.equal(policy.matchesToolPathRule('alias.txt', target, undefined, ruleOptions), false);
  assert.equal(policy.matchesToolPathRule('alias.txt', target, true, ruleOptions), true);
  await target.assertUnchanged(fs);
  entries.set('/project/alias.txt', '/project/other.txt');
  await assert.rejects(target.assertUnchanged(fs), /changed before execution/);

  const missing = await policy.resolveToolPath(fs, 'new/subdir/file.txt', options, credentials);
  assert.equal(missing.path, '/project/new/subdir/file.txt');
  await missing.assertUnchanged(fs);
});

test('credential filtering is specific to the configured app home and its real paths', async () => {
  const fs = fakeFs(new Map([
    ['/', '/'], ['/app', '/app'], ['/project', '/project'],
    ['/app/mcp.json', '/app-data/mcp-config'], ['/app-data/mcp-config', '/app-data/mcp-config'],
  ]));
  const target = await policy.resolveToolPath(fs, 'config.toml', options, credentials);
  assert.equal(target.isCredentialPath('/project/config.toml'), false);
  assert.equal(target.isCredentialPath('/app/config.toml'), true);
  assert.equal(target.isCredentialPath('/app-data/mcp-config'), true);
  assert.equal(target.isCredentialPath('/app/credentials/mcp/grant.json'), true);
});

test('MCP credential grants match source and target and stdio projects inherit only allowed values', () => {
  const config = { transport: 'stdio', command: 'node' };
  const origin = '/project/.mcp.json';
  const tagged = policy.tagMcpCredentialOrigin(config, origin);
  const grants = [{ server: 'sample', target: 'node', origin, variables: ['MCP_TEST_VALUE'] }];
  assert.equal(policy.mcpGrantedVariables(config, 'sample', grants), undefined);
  assert.deepEqual([...policy.mcpGrantedVariables(tagged, 'sample', grants)], ['MCP_TEST_VALUE']);
  assert.equal(policy.mcpGrantedVariables({ ...tagged, command: 'python' }, 'sample', grants).size, 0);
  const env = policy.mergeStdioEnv({ SET_BY_SERVER: 'configured' }, {
    PATH: '/usr/bin', MCP_TEST_VALUE: 'allowed', OTHER_TEST_VALUE: 'filtered',
  }, policy.mcpGrantedVariables(tagged, 'sample', grants));
  assert.deepEqual({ ...env }, { PATH: '/usr/bin', MCP_TEST_VALUE: 'allowed', SET_BY_SERVER: 'configured' });
});

test('a delegated agent follows active plan mode before automatic shell approvals', async () => {
  const parentPlan = { isActive: true };
  const child = {
    isActive: false,
    agentCtx: { agentId: 'child' },
    agentLifecycle: { handleOf: id => {
      assert.equal(id, 'main');
      return { accessor: { get: () => parentPlan } };
    } },
  };
  const getEffectivePlan = Object.getOwnPropertyDescriptor(policy.AgentPlanService.prototype, 'hasEffectivePlan').get;
  const instantiation = {
    invokeFunction: callback => callback({ get: () => ({ hasEffectivePlan: getEffectivePlan.call(child) }) }),
    createInstance: policyClass => ({
      name: policyClass.name,
      evaluate: () => policyClass.name === 'YoloModeApprovePermissionPolicyService' ? { kind: 'approve' } : undefined,
    }),
  };
  const permissions = new policy.AgentPermissionPolicyService(instantiation, { args: {} });
  const context = { toolCall: { name: 'Bash' } };
  assert.equal((await permissions.evaluate(context)).result.kind, 'ask');
  parentPlan.isActive = false;
  assert.equal((await permissions.evaluate(context)).result.kind, 'approve');
});
