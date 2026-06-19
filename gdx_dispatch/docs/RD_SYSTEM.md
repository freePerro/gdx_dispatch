# R&D System — Three Departments

## R&D AI (Models, Performance, Costs, Opportunities)
```bash
python gdx_dispatch/tools/rd_ai_research.py              # Full report
python gdx_dispatch/tools/rd_ai_research.py local        # GPU/model audit
python gdx_dispatch/tools/rd_ai_research.py helpers      # Helper dispatch win rates
python gdx_dispatch/tools/rd_ai_research.py app          # Tenant-facing AI features
python gdx_dispatch/tools/rd_ai_research.py costs        # Cost analysis
python gdx_dispatch/tools/rd_ai_research.py recommendations
```
Output: `ai-queue/rd/ai/latest_report.md`

## R&D Product (Features, Competitors, Market)
```bash
python gdx_dispatch/tools/rd_research.py
```
Output: `ai-queue/rd/product/research/latest_brief.md`

## R&D Operations (Security, Errors, Performance, Reliability)
```bash
python gdx_dispatch/tools/rd_feedback_loop.py --run-tests
python gdx_dispatch/tools/drift_scanner.py
python gdx_dispatch/tools/test_red_light.py check
```
Output: `ai-queue/rd/operations/latest_report.md`. Nightly drift scan 3am UTC.

## Development Feedback Loop (After Every Test Suite)
```bash
python gdx_dispatch/tools/rd_feedback_loop.py /tmp/test_results.txt
python gdx_dispatch/tools/rd_feedback_loop.py --run-tests
```
Outputs: `ai-queue/rd/next_session_brief.md`, `ai-queue/rd/latest_report.md`.

## Drift Scanner (Before Every Deploy)
```bash
python gdx_dispatch/tools/drift_scanner.py 2>&1 | tee /tmp/drift_scan_results.txt
semgrep --config gdx_dispatch/tools/semgrep_gdx_rules.yml gdx/ --error
```
Exit 0 = clean. Fix all violations before deploy.

## Test Gate (After Every Change)
```bash
.venv/bin/pytest gdx_dispatch/tests/ -v --tb=short -q 2>&1 | tee /tmp/gdx_test_results.txt
```
Frontend: `cd gdx_dispatch/frontend && npx vitest run`.

## Quality Gates (Automated)
- `gdx_dispatch/tools/pre_commit_test_gate.sh` — blocks commit if pass count decreased; baseline in `.test_baseline`.
- `gdx_dispatch/tools/test_red_light.py` — `check` / `status` / `clear`; `.tests_red` marker means don't modify non-test code.
- `gdx_dispatch/tools/session_checkpoint.py` — `save` / `show` to capture/restore session state.
