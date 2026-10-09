# claude-plugins

Storj-managed [Claude Code plugin](https://code.claude.com/docs/en/plugins) marketplace.

## Install

```
/plugin marketplace add storj/claude-plugins
/plugin install storj-developer@storj-plugins
```

### Satellite production agents

`storj-developer` includes agents and skills for inspecting production satellite peers:

- agent `satellite-investigator` — root-cause questions about a peer ("why is repair slow in eu1 since 10:00?")
- agent `grafana-analyst` — general Grafana queries that return only the conclusion
- skills `satellite-observability`, `satellite-peers` (per-peer cards), `satellite-infra`, `monkit-metrics`

Setup:
1. Grafana MCP server pointed at grafana.storj.tools, with a **Viewer** service-account token
   (read-only — the agents never need write access). Keep the token in your own MCP config, never in this repo.
2. Clones of `storj/storj` and `storj/infra` (default paths `~/git/storj/storj`, `~/git/storj/infra`).
3. Optional: read-only `kubectl` contexts for the satellite clusters.

Peer cards exist for `api` and `repair`. To add one, follow the format in
`plugins/storj-developer/skills/satellite-peers/SKILL.md`; every query in a card must be verified against prod.

### Recommended extras

- **andrej-karpathy-skills** — LLM coding-mistake guidelines, sourced from [multica-ai/andrej-karpathy-skills](https://github.com/multica-ai/andrej-karpathy-skills)
  ```
  /plugin install andrej-karpathy-skills@storj-plugins
  ```
- **gopls-lsp** — `/plugin install gopls-lsp@claude-plugins-official` (requires [`gopls`](https://pkg.go.dev/golang.org/x/tools/gopls))
- **[Playwright CLI](https://github.com/microsoft/playwright-cli)** — browser automation for coding agents
  ```
  npm install -g @playwright/cli@latest
  cd && playwright-cli install-browser --with-deps --only-shell && playwright-cli install --skills
  ```

## License

MIT
