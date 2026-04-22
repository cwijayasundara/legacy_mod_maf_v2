# Role: COBOL → Java translator (dispatch stub)

**Not used directly.** Phase 4 split the translator into three
target-style prompts:

- ``translator_batch.md`` — JCL-invoked programs → Spring Batch.
- ``translator_cics.md`` — CICS transactions → Spring MVC.
- ``translator_subroutine.md`` — CALLed subroutines → ``@Service`` beans.

The translator orchestrator (``translator.py``) classifies each
program via ``target_style.classify_program`` and loads the matching
prompt directly. This file is kept for backward compatibility with
any external references to the Phase 1 path.
