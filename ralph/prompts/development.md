# Role: developer — iteration {{iteration}} on task `{{task_slug}}`

## Task: {{task_title}}
{{task_body}}

## Product context (PRD, for reference)
{{prd}}

## Feedback from the previous iteration
{{feedback}}

## Answer from the human (if you asked something)
{{human_answer}}

## How to work
1. Read `{{run_dir}}/notes.md` and look at `git log --oneline -15` and `git diff {{base_ref}}...HEAD --stat`.
2. Pick the single most valuable next step for THIS task and do it. Small increments.
3. Write or update unit tests for behaviour you change.
4. Run the gates (`{{gates_cmd}}`) and fix failures.
5. Update `{{run_dir}}/notes.md`.

Use DONE only when every acceptance criterion is met and the gates pass. Otherwise use CONTINUE.
