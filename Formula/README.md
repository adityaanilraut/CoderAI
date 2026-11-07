# Homebrew tap

This repository also serves as CoderAI's Homebrew tap. `coderai.rb` is generated
from the four macOS/Linux standalone release ZIPs and their verified SHA-256
checksums; do not hand-edit it or use placeholder checksums.

The first published stable Bun release (0.6.0 or later) enables the tap. The
`Update Homebrew tap` workflow writes `Formula/coderai.rb` to the default branch
after a stable release is published. It can also be run manually with the latest
published stable tag. Drafts and prereleases are never installed through the tap.
The workflow uses this repository's `GITHUB_TOKEN`; no separate tap repository or
personal access token is needed. Repository rules must allow the workflow to
commit this file to the default branch. If branch protection requires a pull
request, generate the formula locally and merge it through the normal review path.

To generate the formula locally after downloading all four macOS/Linux ZIPs and
their `.sha256` files into `release-assets/`:

```sh
bun scripts/build-homebrew.mjs --version 0.6.0 \
  --assets-dir release-assets --output Formula/coderai.rb
```

The standalone release workflow also attaches `coderai.rb` to its draft release
for review. Generation fails if any archive is missing, has an invalid checksum
file, or differs from its checksum. All platforms must finish building before
the formula can be generated.

After the generated formula is on the default branch, users install with:

```sh
brew tap adityaanilraut/coderai https://github.com/adityaanilraut/CoderAI.git
brew install adityaanilraut/coderai/coderai
```

The explicit repository URL is required because this repository is named
`CoderAI`, rather than Homebrew's conventional `homebrew-coderai`.
