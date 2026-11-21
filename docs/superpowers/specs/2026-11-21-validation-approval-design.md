# Validation command approval (issue #9)

## Why

Every validation command in a plan runs as `sh -c` in the session container after the agent
finishes. The commands come from the planning model, and nothing asks the user first. In an
end-to-end run a model wrote an English sentence with backticks as a command; the shell ran the
backtick text, started a server, and the session failed after a 5-minute timeout although the
agent's edit was correct. It stayed contained, so this is a correctness and trust problem.

The existing `source` field (`observed` / `proposed`) cannot serve as approval: on models without
structured output the model's own JSON goes straight to `plans.validate` and can claim
`observed`, and the user can switch it in the editor.

## Goals

- A validation command runs only if the user approved it.
- Approval is part of the immutable plan version, so the approved text is exactly the text
  that runs, and the session's content hash covers it.
- A plan with unapproved checks still starts; those checks are skipped, not failed.
- The user sees which checks will run and which will be skipped before starting.

## Non-goals

- Rejecting or rewriting prose commands on the server (the approval gate covers safety; a
  heuristic there would cause false rejections and repair loops).
- Approving checks in the launch step (approach B) or per session.
- Changing how approved commands execute (container, user, network, timeouts stay as they are).

## Approaches

- **A. Approval per check in the plan (chosen).** Each validation item gets `approved`. Model
  output is forced unapproved; only a manual save can approve. Sessions skip unapproved checks.
- B. Approve at launch: more launch UI, and Retry must re-ask or carry the choice; approval would
  live outside the plan version.
- C. Any manual save approves all checks: a typo fix in a step would silently approve a prose
  command. Rejected.

The user picked A, with plans still starting and skipping unapproved checks with a warning.

## Design

- **Plan content.** Validation items gain an optional boolean `approved`. Missing means not
  approved, so existing plan versions keep their hashes and their checks are skipped until the
  user approves them in a new version. `plans.validate` rejects a non-boolean value.
- **Who can approve.** `_insert` sets `approved: false` on every validation item for any
  version whose provenance is not `manual` (`model`, `amendment`). Manual saves through the API
  keep the value the user set. Editing a command's text in the editor keeps its approval state
  as the user left it (my call: the user is looking at the text they typed).
- **Readiness.** A plan with any unapproved check gets the warning `validation_unapproved`
  (not a blocking reason).
- **Sessions.** `_validate` runs only items with `approved is True`. Others are recorded in
  `validation_runs` as `status='not_run'`, `required=0`, reason `not_approved` in
  `environment_json`, and emit the existing completion event with that reason. A session whose
  checks were all skipped can still complete. `not_approved` takes precedence over the reasons
  used when nothing ran (for example `agent_not_completed`).
- **Outcome.** Skipped checks count toward `validation.total` but not `passed`, so the session is
  not `fully_validated` and automatic publication (`on_success_draft`) does not happen; manual
  draft publication is unaffected. In the sessions view, a summary whose only `not_run` checks
  are `not_approved` is shown as skipped, not as failing.
- **Plan view.** Each check shows whether it will run. Unapproved: "Not approved — this check
  will be skipped. Approve it by editing the plan." Approved: "Approved — runs as a shell
  command in the session container."
- **Editor.** Each validation row gets a "Run this check" checkbox (labelled as running the text
  as a shell command). When the command looks like prose (contains a sentence-ending period
  followed by a space, or starts with a capitalized common English word and contains several
  spaces), show a warning next to it. Advisory only.
- **Sessions view.** Skipped checks show "Skipped: not approved in the plan."
- **Changelog** entry under Unreleased.

## Tests

- Model-sourced versions store `approved: false` even if the model output said `true`.
- Manual save keeps `approved: true`; non-boolean is rejected; missing is accepted.
- Readiness warns `validation_unapproved` and stays `ready` otherwise.
- A session with one approved and one unapproved check runs only the approved one; an
  unapproved command containing backticks and `$(...)` never reaches the runner.
- Web asset tests for the plan view and editor strings.

## Left to the builder

Exact copy placement in the plan view and editor, and the prose heuristic's details, within the
above.
