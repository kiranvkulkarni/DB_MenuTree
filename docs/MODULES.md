# Code Map

Where each thing lives and what it is responsible for. Pair with
[ARCHITECTURE.md](ARCHITECTURE.md), which explains *why*.

## Data flow

```
                    ┌──────────────────── back-end A ────────────────────┐
  device ──► droidbot ──► droidbot_out/{utg.js, events/, states/}
                              │
                              ▼
                        utg_parser.py ─────────┐
                                               │
                    ┌──────────────────── back-end B ──────┼─────────────┐
  device ──► replay_explorer.py ──► u2_out/menutree.json   │
                              │                            │
                              ▼                            ▼
                     menutree_loader.py ──────────►  MenuTree  ◄─────────┘
                                                        │
                             ┌──────────────────────────┴────────────┐
                             ▼                                       ▼
                     path_emitter.py                          coverage.py
                             │                                       │
                             ▼                                       ▼
                     uvta_writer.py ──► out/*.uvta            reports/*.json
                                                                     │
                                                                     ▼
                                                               exit 0 / 1 / 2
```

## Core model

| File | Responsibility |
|---|---|
| `src/parser/menu_tree.py` | `MenuTree`, `MenuState`, `Transition`, `Selector`. BFS from root, reachability, dead ends, ambiguity classification, activity-name normalisation. **Back-end agnostic.** |
| `src/parser/selectors.py` | `SelectorResolver` — view → selector, including Compose descendant-label resolution. Shared by both back-ends so they cannot drift. |

## Back-end A — DroidBot

| File | Responsibility |
|---|---|
| `src/crawler/droidbot_runner.py` | Runs DroidBot. Preflight (adb, droidbot, device auth, APK), `pm clear`, explicit `-count`/`-timeout`, streamed logging. |
| `src/crawler/apk_resolver.py` | Package name → local APK via `adb pull`. Device verification. Refuses split-only installs explicitly. |
| `src/parser/utg_parser.py` | `utg.js` + `events/` + `states/` → `MenuTree`. The join, plus the five repairs described in ARCHITECTURE §4. |
| `tools/patch_droidbot.py` | Patches installed DroidBot for androguard 4.x. **Re-run after any droidbot reinstall.** |

## Back-end B — replay explorer

| File | Responsibility |
|---|---|
| `src/crawler/hierarchy.py` | uiautomator XML → normalised views. **`state_key` — the state abstraction.** `interactive_views`, `center_of`. |
| `src/crawler/device_driver.py` | `DeviceDriver` protocol; `U2Driver` (uiautomator2) and `AdbDriver` (no-agent fallback). |
| `src/crawler/replay_explorer.py` | The explorer: frontier, replay, forward continuation, quiescence capture, checkpointing. |
| `src/parser/menutree_loader.py` | `menutree.json` (format `menutree/1`) → `MenuTree`. |
| `tools/explore_u2.py` | CLI runner + side-by-side comparison against a DroidBot output dir. |

## Shared downstream

| File | Responsibility |
|---|---|
| `src/generator/path_emitter.py` | Graph paths → `TestCase`s. One test per actionable transition; steps are the shortest path to its source plus that transition. Deterministic ordering. |
| `src/generator/uvta_syntax.py` | **Every piece of UVTA surface syntax.** Change output here and nowhere else. Unconfirmed forms marked `UNVERIFIED`. |
| `src/generator/uvta_writer.py` | Writes and structurally validates the suite. Refuses to write an empty suite. |
| `src/analysis/coverage.py` | Coverage report, baseline diffing, gate evaluation. |
| `src/logging_setup.py` | Timestamped stream + per-run file logging. |
| `main.py` | Orchestrator for the DroidBot path. CLI, exit codes. |

## Tests

| File | Runs without a device? | Covers |
|---|---|---|
| `tests/test_hierarchy.py` | yes | XML parsing, state-key stability, Compose selectors, bounds |
| `tests/make_fixture.py` | yes | Writes a synthetic `droidbot_out/` in DroidBot's exact format |

Run both before any device work — they take seconds.

```bash
python tests/test_hierarchy.py
python tests/make_fixture.py
```

## Common tasks

| Task | Go to |
|---|---|
| Change UVTA output syntax | `generator/uvta_syntax.py` |
| Change what counts as "the same screen" | `hierarchy.py :: state_key` |
| Change how a view becomes a selector | `parser/selectors.py` |
| Change gate thresholds / regression rules | `analysis/coverage.py` |
| Add a device back-end | implement `DeviceDriver` in `device_driver.py` |
| Add a crawler back-end | emit `menutree.json`, reuse `MenuTreeLoader` |
| Add an action type (scroll, text input) | `hierarchy.py` (enumerate) + `replay_explorer.py` (`_perform`) + `uvta_syntax.py` (render) |

## Config

`config.yaml.example` is the annotated reference. Local copies
(`config.yaml`, `config.*.yaml`) are gitignored.

Sections: `logging`, `crawler`, `parser`, `generator`, `coverage`, `llm`.

The settings most worth understanding before a run:

- `crawler.timeout` / `crawler.count` — exploration budget. `600` seconds is
  roughly 600 events, nowhere near exhaustive for a large app.
- `crawler.clear_app_data` — reproducibility. Off means runs are not
  comparable.
- `parser.resolve_descendant_labels` — required for Compose.
- `coverage.min_activity_coverage` — see ARCHITECTURE §9.1 before setting this
  above zero for a single-Activity app.
