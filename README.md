
## AI ops robots (GHA)

Automated via opencode-go worker (see .github/workflows/):
- **PR review** — every PR gets a fail-closed review; clean PRs auto-merge (squash).
- **Issue triage** — new issues labeled+prioritized; 6-hourly sweep.
- Override model with AI_REVIEW_MODEL / AI_TRIAGE_MODEL secrets. Deepseek-v4-pro region-gated; default glm-5.1.
