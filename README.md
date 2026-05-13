# claude-plugins

Storj-managed [Claude Code plugin](https://code.claude.com/docs/en/plugins) marketplace.

## Install

```
/plugin marketplace add storj/claude-plugins
/plugin install storj-developer@claude-plugins
```

### Recommended extras

- **andrej-karpathy-skills** — `/plugin install andrej-karpathy-skills@claude-plugins` (LLM coding-mistake guidelines, sourced from [multica-ai/andrej-karpathy-skills](https://github.com/multica-ai/andrej-karpathy-skills))
- **gopls-lsp** — `/plugin install gopls-lsp@claude-plugins-official` (requires [`gopls`](https://pkg.go.dev/golang.org/x/tools/gopls))
- **[Playwright CLI](https://github.com/microsoft/playwright-cli)** — browser automation for coding agents
  ```
  npm install -g @playwright/cli@latest
  cd && playwright-cli install-browser --with-deps --only-shell && playwright-cli install --skills
  ```

## License

MIT
