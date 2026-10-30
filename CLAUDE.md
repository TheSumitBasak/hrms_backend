Implement this feature in my existing FastAPI backend using the FastAPI Rules, Clean Architecture, API Design, Testing/TDD, and Code Review skills.

Before coding:
1. Inspect the existing project structure and relevant files.
2. Identify the existing conventions, dependencies, database patterns, and API contracts.
3. Propose a minimal implementation plan and identify potential regressions.

Implementation requirements:
- Follow the existing architecture; do not rewrite unrelated modules.
- Keep route handlers thin and separate business logic from persistence.
- Use SQLAlchemy 2.x, Pydantic v2, and the project's existing Alembic conventions.
- Avoid code smells, duplicated logic, unnecessary abstractions, N+1 queries, and broad exception handling.
- Validate authorization, input boundaries, transaction handling, and error responses.
- Add unit and integration tests, including failure and regression cases.
- Do not change existing API contracts or database schemas unnecessarily.

Before finishing:
- Review the final diff for correctness, maintainability, security, and backward compatibility.
- Run relevant tests, linting, and type checks.
- Fix issues found and rerun the affected checks.
- Report changed files, tests actually executed, results, and any remaining risks.
