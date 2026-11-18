import { readFile } from 'node:fs/promises';

const number = '(?:0|[1-9][0-9]*)';
const identifier = `(?:${number}|[0-9]*[a-zA-Z-][0-9a-zA-Z-]*)`;
const supportedVersion = new RegExp(`^${number}\\.${number}\\.${number}(?:-(${identifier}(?:\\.${identifier})*))?$`);

export function releaseKind(tag, version) {
  const match = supportedVersion.exec(version);
  if (!match) throw new Error('Release version must be SemVer without build metadata.');
  if (tag !== `v${version}`) throw new Error(`Release tag ${tag} does not match package version v${version}.`);
  return match[1] ? 'prerelease' : 'stable';
}

if (import.meta.main) {
  const { version } = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'));
  console.log(releaseKind(process.argv[2], version));
}
