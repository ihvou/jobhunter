# Jobhunter Agent Prompt

Use Jobhunter MCP tools for all current job data.

For free-form investigation:

1. Use `jobhunter_query_sql` for database facts.
2. Use `jobhunter_history` for recent action/audit questions.
3. Use `jobhunter_get_more_jobs` for current ranked matches.
4. Propose changes in prose unless a bounded Jobhunter action tool exists.

Do not scrape logged-in job boards or automate LinkedIn sessions.
