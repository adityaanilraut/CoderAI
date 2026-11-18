import { readFile, readdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { thirdPartyLicenseFile } from './release-archive.mjs';

const noticeName = /^(?:licen[sc]es?|copying|notice|copyright|unlicense)(?:[_.-].*)?$/i;
const vendorFile = name => new URL(`../third-party-licenses/${name}`, import.meta.url);

function packageRoots(metafiles) {
  const roots = new Set();
  for (const { outputs } of metafiles) {
    for (const output of Object.values(outputs)) {
      for (const [path, { bytesInOutput }] of Object.entries(output.inputs)) {
        const normalized = path.replaceAll('\\', '/');
        const marker = 'node_modules/';
        const start = normalized.lastIndexOf(marker);
        if (!bytesInOutput || start === -1) continue;
        const parts = normalized.slice(start + marker.length).split('/');
        const packagePath = parts.slice(0, parts[0].startsWith('@') ? 2 : 1).join('/');
        roots.add(normalized.slice(0, start) + marker + packagePath);
      }
    }
  }
  return [...roots].sort();
}

async function packageNotices(metafiles) {
  const packages = new Map();
  for (const root of packageRoots(metafiles)) {
    const { name, version, license } = JSON.parse(await readFile(join(root, 'package.json'), 'utf8'));
    const id = `${name}@${version}`;
    if (packages.has(id)) continue;
    const files = (await readdir(root, { withFileTypes: true }))
      .filter(entry => entry.isFile() && noticeName.test(entry.name))
      .map(entry => entry.name).sort();
    const text = files.length
      ? await Promise.all(files.map(async file => `--- ${file} ---\n${await readFile(join(root, file), 'utf8')}`))
      : [await readFile(vendorFile(`${name.replaceAll('/', '-')}-${version}.LICENSE`), 'utf8').catch(error => {
        throw new Error(`No license text for bundled dependency ${id}`, { cause: error });
      })];
    packages.set(id, `${id} (${license || 'see terms below'})\n\n${text.join('\n')}`);
  }
  return [...packages.entries()].sort(([a], [b]) => a.localeCompare(b, 'en')).map(([, body]) => body);
}

export async function writeNativeLicenses(out, metafiles, clipboard) {
  const bun = process.versions.bun;
  const sections = [
    'Third-party notices for the CoderAI standalone distribution',
    `Bun ${bun} runtime and linked library notices\n\n${await readFile(vendorFile(`bun-${bun}.LICENSE.md`), 'utf8')}`,
    ...await packageNotices(metafiles),
  ];
  if (clipboard) {
    sections.push(await readFile(vendorFile('clipboard-0.3.9.NOTICE'), 'utf8'));
    sections.push(`clipboard-rs 0.3.1\n\n${await readFile(vendorFile('clipboard-rs-0.3.1.LICENSE'), 'utf8')}`);
  }
  for (const { outputs } of metafiles) {
    for (const path of Object.keys(outputs).filter(path => path.endsWith('.LEGAL.txt'))) {
      const text = (await readFile(path, 'utf8')).trim();
      if (text) sections.push(`License comments retained from ${path.split('/').at(-1)}\n\n${text}`);
    }
  }
  await writeFile(join(out, thirdPartyLicenseFile), sections.join('\n\n========================================\n\n') + '\n');
}
