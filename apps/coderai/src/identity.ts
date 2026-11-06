import manifest from '../../../package.json' with { type: 'json' };

export const identity = { productName: 'coderai', version: manifest.version, platform: 'kimi_code_cli' };
