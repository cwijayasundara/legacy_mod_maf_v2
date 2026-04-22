# Authoring a new migration cartridge

## Steps

1. Create `maf_generic_migrator_v1/cartridges/<your_id>/` with an `__init__.py`.
2. Add `cartridge.py` exposing a module-level `CARTRIDGE = YourCartridge()`.
3. Subclass `maf_generic_migrator_v1.platform_core.cartridge.MigrationCartridge`:
   - Set `id`, `description`, `version`, `source`, `target`.
   - Implement `adapters()` — return a dict of language -> `LanguageAdapter`.
   - Implement `unit_classifier(repo_root)` — return the list of unit roots.
   - Implement `translator_agents()` — return `AgentSpec` list with prompt paths.
   - Optionally implement `grapher_hints`, `complexity_patterns`, `recipes`,
     `service_map`, `idiom_map`, `test_framework_map`, `rubrics`, `corpus_dir`,
     `verify_unit`, `verify_wave`.
4. Create `prompts/<role>_<lang>.md` files referenced by your `AgentSpec` entries.
5. Add `rubrics/<name>.yaml` declaring rubric rows with dotted-path scorer refs.
6. Add `corpus/fixtures/` with at least 3 golden fixtures.
7. Add tests under `maf_generic_migrator_v1/cartridges/<your_id>/tests/`.
8. Run `pytest maf_generic_migrator_v1/tests/conformance/ -k <your_id>` to confirm your cartridge
   satisfies the platform contract.

## Rules of thumb

- **Deterministic first.** If a rewrite can be expressed as an AST rule,
  write it as a recipe. LLM translators are only for what rules can't cover.
- **One translator per source language** (or per source-target slice). Don't
  try to make a single omni-translator.
- **Keep prompts declarative.** Put service / idiom mappings in
  `service_map()` / `idiom_map()`, not in prompt text. The platform injects
  them into the prompt at run time.
- **Emit artifacts the platform can read.** The translator writes files under
  the unit workdir; the scorers read that workdir to compute scores.
- **Don't reach into `platform_core` beyond the public imports.** If the
  cartridge needs something the core doesn't expose, open an issue / PR.

## Quickstart template

```python
from maf_generic_migrator_v1.platform_core.cartridge import AgentSpec, EcosystemSignature, MigrationCartridge

class MyCartridge(MigrationCartridge):
    id = "my_cartridge"
    source = EcosystemSignature(language="python", version="2.7")
    target = EcosystemSignature(language="python", version="3.11")

    def adapters(self):
        from maf_generic_migrator_v1.adapters.python import PythonAdapter
        return {"python": PythonAdapter()}

    def unit_classifier(self, repo_root):
        return [p for p in repo_root.iterdir() if p.is_dir()]

    def translator_agents(self):
        return [AgentSpec(
            name="translator",
            role="translator",
            system_prompt_path="prompts/translator.md",
        )]

CARTRIDGE = MyCartridge()
```
