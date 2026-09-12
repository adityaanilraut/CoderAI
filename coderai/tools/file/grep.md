A powerful search tool based on ripgrep.

Search file contents with a ripgrep regular expression. Returns matching lines with line numbers, grouped by file. Returns the first 250 matches inline; a capped result reports where the complete match list was saved. Use read on a matched file for surrounding context.

**Tips:**
- ALWAYS use Grep tool instead of running `grep` or `rg` command with Shell tool.
- Use the ripgrep pattern syntax, not grep syntax. E.g. you need to escape braces like `\\{` to search for `{`.
- Hidden files (dotfiles like `.gitlab-ci.yml`, `.eslintrc.json`) are always searched. To also search files excluded by `.gitignore` (e.g. `node_modules`, build outputs), set `include_ignored` to `true`. Sensitive files (such as `.env`) are still skipped for safety, even when `include_ignored` is `true`.
- `output_mode` selects the result shape: `content` (matching lines, default), `files_with_matches` (file paths only), `count_matches` (per-file counts). Use `head_limit`/`offset` to page through large result sets.
