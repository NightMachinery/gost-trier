# xray-tui YAML TUI Plan

## Summary

Add `xray-tui`, a Textual TUI that reads hand-edited YAML, manages grouped subscription/config sources, caches refreshed subscriptions, tests/samples configs, and runs the selected Xray config through local SOCKS and HTTP listeners.

Each subscription appears as its own subgroup. Names are optional, protocols are shown, all hotkeys are customizable, the layout adapts to narrow terminals, and Xray runs in tmux when available for manual inspection.

## Public Interface

- Add entrypoint: `xray-tui = "gost_trier.xray_tui:main"`.
- Add dependencies: `textual` and `PyYAML`.
- CLI defaults:
  - `--address=127.0.0.1`
  - `--socks-port=1080`
  - `--http-port=2080`
  - repeatable `--test-url`, default `https://api.ipify.org`
  - `--config=~/.xray-tui/config.yaml`
  - `--python-config=<config-dir>/config.py`, defaulting next to YAML
  - `--no-python-config`
  - `--tmux-session=xray-tui-s{SOCKS_PORT}-h{HTTP_PORT}`
  - `--stop-on-exit`, default true, with `--no-stop-on-exit`
  - `--sub-auto-refresh=1h`
  - `--rotate-refresh=15m`
  - `--sample=100`
  - `--true-color=auto|on|off`
  - `--dark-mode=auto|on|off`
  - `--light-theme=<name>`
  - `--dark-theme=<name>`
- If YAML is missing, create parent dirs plus an example config, print schema/key help, then exit successfully.

## YAML, Naming, And State

```yaml
groups:
  - name: default
    subscriptions:
      - url: https://example.com/sub.txt
      - name: backup
        url: https://example.com/backup.txt
    configs:
      - link: direct://
      - link: vless://00000000-0000-0000-0000-000000000000@example.com:443?security=tls#decoded%20name
      - path: ~/xray-configs/example.json
```

- UI hierarchy: YAML groups, one subgroup per subscription, plus `Manual configs`.
- Display name order: explicit `name`, URL-decoded `#fragment`, `host:port`, stable `config-<short-id>`.
- Subscription subgroup name order: explicit `name`, URL host/path label, stable `subscription-<short-id>`.
- Protocol display: link scheme, or first usable JSON outbound protocol with `json` fallback.
- Store cache/runtime state outside YAML:
  - per-subscription cache and last refresh timestamp
  - active group/subgroup/config id
  - last test results and selected fastest config
- Use stable IDs from group/subscription source plus config link/path hash, so cached results survive renames.

## Hotkeys

- Navigation defaults:
  - `j` / `down`: next row
  - `k` / `up`: previous row
  - `h` / `left`: focus/collapse group navigation
  - `l` / `right`: focus config table or expand navigation
  - `g` / `G`: first/last row
  - `tab` / `shift+tab`: cycle focus
  - `[` / `]`: previous/next subgroup
  - `{` / `}`: previous/next top-level group
  - `enter`: select focused group/subgroup/config
  - `esc`: clear filter or cancel chord
- Action defaults:
  - `q`: quit
  - `/`: filter/search
  - `r`: refresh current subgroup if it is a subscription, otherwise current group subscriptions
  - `t`: test/rotate now
  - `a`: toggle auto-rotate
  - `SPC r a`: refresh all subscriptions
  - `SPC r g`: refresh current group
  - `SPC r s`: refresh current subscription subgroup
  - `SPC t a`: test sampled configs now
  - `SPC x r`: restart active Xray
  - `SPC x a`: show tmux attach command/session info
- Python config API:
  - load trusted local `config.py` if it exists
  - expose `configure(hotkeys)`
  - assigning `None` disables an action
  - unknown action ids and duplicate bindings fail fast

## Runtime Behavior

- Prefer tmux when available:
  - create/reuse `--tmux-session`
  - run one foreground `xray run -c <temp-config>` window for the active config
  - restart that window on explicit selection or auto-rotate switch
  - show attach command in the status/details panel
- If tmux is unavailable, fall back to managed detached/subprocess execution using the existing session-management style.
- With default `--stop-on-exit`, stop the active Xray tmux window/process on normal TUI exit. With `--no-stop-on-exit`, leave the tmux session/process running.
- For share links, reuse existing Xray conversion/building helpers and force SOCKS/HTTP listeners from CLI.
- For JSON files, load JSON and replace inbounds with forced SOCKS/HTTP listeners.
- On startup, run the last active config if still valid; otherwise wait for explicit selection or rotation.
- Auto-rotate samples up to `--sample=100` from the selected subgroup by default; selecting a top-level group samples across all its subgroups.
- Ranking tests all `--test-url` values and chooses the config with the fastest successful URL.
- Refresh subscriptions direct/no-proxy first; if that fails, retry with normal proxy environment handling; if both fail, keep old cache.

## TUI Design

- Wide layout, default at `>=100` columns: left expandable group/subgroup tree plus main config table.
- Narrow layout below `100` columns: top group tabs, second-row subgroup tabs, full-width config table.
- If tabs overflow, make them horizontally scrollable and keep `/` search available.
- Main table columns: name, protocol, source, status, latency.
- Bottom status bar: listeners, active group/subgroup/config, tmux session, refresh/test state.
- Key hint bar is always visible and context-aware; chord prefix and remaining keys are shown; wrong key cancels; no timeout.
- Add 3 dark and 3 light themes, with true-color and dark-mode auto-detection including Kitty best-effort hints.

## Suggestions And Defaults

- Tmux-by-default is worth doing; it improves debuggability and fits the existing project behavior.
- Keep `--stop-on-exit` true by default so `xray-tui` does not unexpectedly leave a proxy running.
- Do not auto-refresh every subscription at startup; refresh stale subscriptions only for the selected scope, and use `SPC r a` for global refresh.
- Show stale-cache warnings without blocking old cached configs.
- Surface JSON inbound replacement in the status/details panel so users are not surprised.
- Document that `config.py` is executable trusted Python.

## Tests And Docs

- Test YAML parsing, subgroup construction, optional name derivation, protocol detection, responsive layout mode selection, navigation bindings, hotkey overrides, disabled hotkeys, duplicate binding errors, tmux session naming, tmux restart/cleanup, `--no-stop-on-exit`, fallback runner, refresh scopes, cache preservation, JSON inbound replacement, rotation ranking, sampling scopes, startup selection, and process cleanup.
- Add focused Textual tests for hierarchy rendering, narrow top-tabs layout, focus movement, group/subgroup navigation, and key/chord behavior where practical.
- Update `README.md` and `docs/usage.md` with YAML schema, subgroup model, naming rules, protocol display, responsive layout behavior, tmux behavior, navigation/action keymap, Python hotkey config, cache/state paths, and examples.

## Atomic Commit Groups

1. Config/cache/state parsing, subgroup model, name/protocol detection, and tests.
2. Hotkey/navigation defaults, Python override loading, disabled/conflict handling, and tests.
3. Xray config generation, JSON inbound replacement, tmux/fallback runner, lifecycle flags, and tests.
4. Subscription refresh, rotation logic, sampling scopes, and tests.
5. Textual TUI responsive hierarchy, focus/navigation behavior, themes, keymap/chords, and TUI tests.
6. Entrypoint, dependencies, README/docs updates, full test run, commit, and push.
