# Taste
- Wants a durable, living architecture/context file kept in the project (consolidating product context, business rules, module map, pipeline and workflow) so future sessions can follow the app's workflow. Confidence: 0.6
- Dislikes blindly guessing/brute-forcing inputs (e.g. iterating IDs); prefers discovering the real values by parsing the actual index/listing page first. Confidence: 0.6
- Wants the assistant to ask clarifying questions whenever anything is unclear rather than proceeding on assumptions, and to phrase those questions in plain, easy-to-understand language. Confidence: 0.6
- Runs commands in Windows PowerShell and expects copy-pasteable example commands with real file paths (no shell placeholders like `<converted.csv>`, which PowerShell rejects as redirection). Confidence: 0.55
- Prefers using DeepSeek (or any OpenAI-compatible endpoint) for LLM calls rather than Anthropic, keeping the provider/model configurable via `.env`. Confidence: 0.6
- Prefers an incremental, plan-first workflow: plan larger changes (plan mode) before implementing, do one milestone/step at a time, and review the changed files/diff and test output before moving on. Confidence: 0.55
- Prefers human-facing dates displayed as MM_DD_YYYY (e.g. 12_02_2026) rather than ISO. Confidence: 0.5
- Wants production automation hosted server-side (VPS/company server, containerized) with a thin authenticated admin web UI over the same database for human review/approval, keeping the local PC for development only. Confidence: 0.5
