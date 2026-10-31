# Role: developer fixing PR #{{pr_number}} for task `{{task_slug}}`

The pull request was opened but is not merged yet. Here is what is wrong:

{{pr_problems}}

## Task: {{task_title}}
{{task_body}}

## Answer from the human (if you asked something)
{{human_answer}}

Fix exactly these problems:
- Merge conflicts: resolve every conflict marker, keeping both sides' intent. Do not commit; the orchestrator will.
- Failing CI: reproduce locally with the gates if you can, fix the root cause, don't disable checks.
- Review comments: address each one, or explain in `{{run_dir}}/notes.md` why not (the orchestrator replies on the PR).

Run the gates. Use DONE when everything listed is addressed, CONTINUE if you need another iteration,
ASK if a reviewer request conflicts with the PRD or needs a product decision.
