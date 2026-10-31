# Rules for agents

<!-- Synced from benchLith's agent-rules/AGENTS.md by `bin/lith rules`.
     Change the rules there; an edit here is overwritten by the next sync. -->

These apply to every agent working in any astrum-code repo: Claude Code
locally or in the cloud, or any other agent tool. They hold whether you
are working in this repo alone or inside the benchLith workspace
(github.com/astrum-code/benchLith), where the repos sit together as
submodules.

## How work is assigned

Several agents work on these repos at once -- Claude Code here and in the
cloud, other tools, agents on other machines -- and every one of them
pushes to GitHub as the same user. So who is doing what is written down
where all of them can read it: the **board**, the issues of
[astrum-code/benchLith](https://github.com/astrum-code/benchLith/issues).
**`roster.toml`** (next to this file) lists the roles: what each owns,
what it must never touch, and the docs it keeps current.

### Your role

- **Know your role before you start.** The user or the coordinator gives
  it in your first message (`henge`, `vistalith`, ...). A pool worker is
  `worker-<something unique>`, e.g. `worker-cloud-3`; one started by
  `bin/lith launch` is named after its pool workspace (`worker-2`, in
  `agents/worker-2`). That name is its role everywhere: `claim:
  worker-2`, `Agent: worker-2` in commits and PRs. A worker takes only
  `role:worker` issues (the coordinator relabels work that needs no
  standing role's context) and, in each repo it touches, follows the
  `never` list of the role that owns it. **No role given: ask, and do
  not start.**
- **A role may have named instances.** When its roster entry sets
  `instances = N` (henge, workbench: 2), up to N agents work that role at once: the
  role itself and `<role>-2` .. `<role>-N`, e.g. `henge-2` in
  `agents/henge-2`. An instance takes the role's issues (`role:henge`)
  and follows its roster entry in full -- owns, never, notes,
  keeps_current -- but claims, commits and opens PRs under its own name
  (`claim: henge-2`, `Agent: henge-2`). By default no two instances work
  the same repo at once, so their PRs do not conflict; the coordinator
  allows it for an issue with the `parallel` label.
- **Read your role in `roster.toml`** -- its `never` list is as binding
  as this file -- and the role that owns each repo you touch.
- **Name yourself after your role** where your tool lets you (a session
  title, a terminal name), so the coordinator can find you.

### Find and claim a task

Work starts from an issue on the board, labelled `role:<role>` (workers:
`role:worker`), `repo:<repo>` for each repo it touches, and one
`status:` -- `ready`, `claimed`, `blocked` or `in-review`.

```bash
bin/lith board --role henge     # what is yours
bin/lith board show 31          # read it all, comments included
```

**Claim it before you touch anything** -- one claim at a time, unless the
coordinator gives you more:

```bash
bin/lith claim 31 --as henge --branch feat/31-catalog-fields
bin/lith board show 31      # then read the comments again
```

`bin/lith claim` labels the issue `status:claimed` and records the
`claim:` comment. **The earliest `claim:` comment holds the issue.** If an
earlier one is not yours, you lost a race: comment `yield: <role>`
(`bin/lith issue comment 31 --body "yield: henge"`) and pick another
issue. A `release:` comment by the holder frees it again. `bin/lith board`
shows the whole board. The board is local; `gh` against GitHub is only the
`--github` breakglass.

### While you work, and when you finish

- **Branch as `<type>/<issue>-<short-name>`** (`feat/31-catalog-fields`),
  the same name in every repo the issue touches.
- **End each commit with `Agent: <role>`** as a trailer, above any
  `Co-Authored-By`.
- **Each PR's description** starts with `Agent: <role>` and links the
  issue: `Closes astrum-code/benchLith#31` when that PR finishes it.
  When several PRs in several repos make up one issue, each says
  `Part of astrum-code/benchLith#31`, and only the last one in the merge
  order says `Closes`. Then move the issue to `status:in-review`.
- **Blocked?** Label `status:blocked` and record what it waits on. On
  another board issue: `bin/lith issue label 31 --add-label status:blocked --add-blocked-by 28`
  (the board tracks it, and the coordinator sees when #28 closes). On
  anything else -- a PR, a decision: a comment `Blocked by <what>`.
- **Can't finish it?** Release it, with the reason:
  `bin/lith issue label 31 --add-label status:ready --remove-label status:claimed`
  and a comment `bin/lith issue comment 31 --body "release: henge -- why"`. Never just walk away from a claim.

### Tiers: the scores

Every issue carries two scores as labels: **`tier:1`-`tier:5`**, how
hard it is, and **`risk:1`-`risk:10`**, its blast radius -- what breaks,
and for whom, if it is wrong. For each, pick the **highest anchor that
applies**:

| Tier | Means |
|---|---|
| 1 | mechanical (a lock bump, a doc fix) |
| 2 | routine, well-trodden |
| 3 | ordinary work inside an existing design |
| 4 | new interfaces, concurrency, cross-repo |
| 5 | protocols, formats, security, hard to undo |

| Risk | Anchor (pick the highest that applies) |
|---|---|
| 1 | docs, comments, a test; nothing a user or another repo sees |
| 2 | a dev tool or script nobody runs in production |
| 3 | one repo's internals; no public API, format or dependent changes |
| 4 | a public API addition, backward compatible; a lock bump of a patch |
| 5 | a behaviour change inside one repo that dependents will notice; a minor lock bump |
| 6 | a change to two or more repos that must merge in order |
| 7 | authentication, authorisation, policy, keys, the vault; anything in the security pass's scope |
| 8 | an on-disk or wire format, a record schema, a ring: every node must agree; hard to undo |
| 9 | data loss or cross-tenant exposure possible if wrong; migrations; deletion and purge paths |
| 10 | production infrastructure or the public mirror: the home cluster's data, DNS, the release that reaches the public org |

The scores drive one table (`SCORE_POLICY` in benchLith's `bin/lith`):

| when | then |
|---|---|
| tier 1-2 / 3 / 4-5 | the agent runs small / standard / deep (model and effort) |
| tier >= 4 or risk >= 7 | a **templet** -- the spec -- is agreed before the code |
| tier >= 3 or risk >= 5 | the pool **reviews** the PR (a full review; below both, a small-class one) |
| tier 5 or risk >= 8 | a **second role reviews** the PR, in addition |
| risk >= 8 | **only the user merges** it (below, the coordinator's rights hold) |
| risk >= 8 | the PR names its **rollback** (`## Rollback`) |

The coordinator scores an issue when it cuts it. `bin/lith launch`
refuses an issue without both scores, and `bin/lith board` shows it
**unscored**. **You may ask for a higher score on your issue -- never set
or change one**: if the work turns out harder or riskier than its scores,
say so on the issue, with why (`outcome: blocked` when headless), rather
than pressing on. Nothing escalates automatically. What a templet is, and what it asks of
the worker, is under "Templets", below.

### Review

Every PR is reviewed before the user's operational review (the merge and
the live run), by the general worker pool -- never by its author. It is
automatic (plan #709):

- **The sweep files the review.** When a run ends `outcome: pr`, the
  launcher files the review issue itself: `role:worker`, `kind:review`,
  the PR's tier and risk, the PR links and bodies, `reviews: #N`, and
  `author: <who>` in its body. Its class is the PR's own, or one up
  (`SCORE_POLICY["review_class"]`). At tier >= 3 or risk >= 5 it is a
  full review; below both, a small-class one (`review_small`). The
  coordinator files nothing by hand for it.
- **The reviewer is a pool worker that is not the author, nor the
  author's instance.** It checks out the PR branch(es) detached, **never
  edits or pushes them**, runs the **code-review skill where its harness
  has one** (Claude Code), and otherwise the **fixed checklist carried in
  its prompt**. Both cover the same checklist, each item a line in its
  result: correctness and the diff's intent; tests present, run through
  `bin/lith test <repo>`, passing (a PR whose diff touches only
  documentation -- no `src/`, `bin/`, `tests/`,
  no package or pipeline file, a templet page included, and its re-review
  -- is gated by `bin/lith docs check`, `bin/lith version check` and the
  secret scan instead; `bin/lith docs-only` says whether it is one); the
  templet's acceptance tests present and **unweakened**; the PR body's
  "tested" and "not tested" claims against the diff; the changelog
  fragment with its `bump:` line, and the version line untouched; docs
  pins updated or `docs-later`; no internal names in a public-bound repo
  (the polish lint); the secret scan; the stated merge order of a
  multi-repo PR; no `poetry.lock` change in a PR that is not a `chore/`
  one; for a PR that touches host-touching code ("Real before fake"), a
  lab report that is current (`bin/lith lab check <repo> --pr N`), and a
  PR without one is refused. It ends `outcome: review` with a
  **`verdict: approve`** or **`verdict: changes`** line, then its
  findings. A review is a run like any other: same rules, same status
  lines.
- **Findings come in three groups** (plan #1420): **must fix** (a bug, a
  missing or weakened test, a false claim, a gate failure caused by the PR's
  diff), **should fix** (real, not blocking) and **note** (wording, taste). The verdict is
  `changes` only when a must-fix finding exists; an `approve` may carry
  should-fix and note findings. The sweep files the should-fix findings of
  an approved review as **one** issue per PR, `follow-ups of <repo>!<n>
  (#<issue>)` (the reviewed issue's role, the small class, `status:ready`,
  linked to the PR), each finding a checkbox line (file, line, the
  finding); a later approved round on the same PR appends to that batch
  while it is open, rather than opening another, and the batch is reviewed
  once like any issue. Notes stay in the review. The outcomes view counts
  review rounds per PR (median and worst) and follow-up batches, not
  findings.
- **A failed gate is must-fix only when the PR's diff causes it**: run the
  failing check at `origin/main` too; identical pages or findings, with none
  touched or caused by the diff, are a **note** naming the owner issue
  (re-pins #2506, #2507, #2508; mechanism #2190), never deciding the verdict,
  and the author names them under **not tested / known** in the PR body.
- **The verdict drives the flow.** `approve`: the issue is labelled
  `status:reviewed` and the review is posted on the PR. `changes`: the
  findings go on the issue, it returns to `status:ready` (with
  `changes-requested`), the next launch resumes the **author on the
  branch** with the review among its comments, and a fresh review follows
  its next `pr`. A review is judged per round: the verdicts since the
  last `review requested:` note on the issue.
- **The second role's review stays, in addition** (tier 5 or risk >= 8):
  the issue is `status:reviewed` only when the pool and the second role
  both approved. Either one's `changes` sends the author back.
- **A templet adds a conformance review** (plan #976). When the issue has
  an attached templet ("Templets", below), the sweep files a second review
  beside the pool's, for the **templet role**: `kind:review`, `reviews: #N`,
  a `kind: conformance` line, and the page. Where the role has two instances
  and the other is free, it is not the instance that wrote the templet
  (otherwise the same, since it reviews against the page, not its own code).
  The pool asks whether the code is right; this asks whether it is what the
  templet says: interfaces as specified; invariants held; every
  `## Acceptance` item a test marked `templet:<issue>` and unweakened;
  boundaries respected; the PR's "not tested" honest against the spec. The
  issue is `status:reviewed` only when the pool and the templet role both
  approved (and the second role, where one is asked); either one's `changes`
  sends the author back. It never edits the PR or the page. The outcomes
  view counts these reviews and their verdicts. It also judges any change
  to the page the worker proposed in the PR (see "Templets", below): a
  `page: approved` or `page: refused -- <why>` line in its result for each
  proposed change, a refusal being a must-fix finding.
- **The user merges reviewed PRs.** `bin/lith board` and `/merge-ready`
  list reviewed PRs as ready to merge and the unreviewed ones apart, under
  a heading of their own, so nothing hides. The coordinator's merge rights
  (a green patch lock bump, a rules-sync PR) hold for the PRs it opens
  itself, which have no review issue, and for an agent's PR only once it
  is `status:reviewed`.
  **The user may merge over `changes`**: the sweep then marks the issue
  `overruled` ("overruled by the user"), and the outcomes view counts it.
- **Before you write the result**, run the checklist yourself: the same
  gates (`bin/lith test <repo>`, `bin/lith version check`, `bin/lith docs
  check`) and the PR body's "tested" and "not tested" sections, so most
  review rounds never happen.

### The sweep: what runs without anyone

`bin/lith sweep` is the mechanical half of the coordinator's work as one
idempotent command (plan #780), run every 20 minutes by `launchd` on the
laptop (`bin/lith sweep install`), later by MeshLith. It is code, not a
session, so it does only what the rules above already decide. Each round:
refreshes the mains; sweeps the agents' results (PRs, questions, reviews,
failures); files the pool review of any PR that has none (loudly, with the
labels checked first: `bin/lith review N` files it again); sends back a PR
that conflicts with `main` only in `poetry.lock`, `CHANGELOG.md` or
`docs/ATLAS.md`; unblocks issues whose blockers closed; bumps the
submodule pointers and the **patch** lock pins as PRs; merges what it may;
and launches by the board's order up to the cap, inside the usage limit,
at any hour and any class. It writes one summary line per round to
`sweep/sweep.log` under the agents folder, and what needs judgement to
`sweep/judgement.json`.

**It merges a PR** at **risk 4 or below** when the pool approved it
(`status:reviewed`, no `changes` verdict in the round), the gate is green,
no `question:` on the issue is unanswered (an `answer:` or `Decided`
comment answers it; a `Taken: <option> -- <why>` line is not a question),
and, for a set of PRs, every member qualifies and the stated order is
followed; it comments on the issue when it merges. **Never** at risk 5 or
above, nor a PR a review flagged, nor an `overruled` one, nor a templet
PR: those stay the user's. After two weeks the figures decide whether
risk 5 joins.

**What stays the coordinator's** (the sweep queues it, never guesses):
triage of an issue with no role or status, scoring, a plan, an agent's
question, a stale claim, a conflict in anything but the files above, and a
review the sweep could not file. A minor lock bump stays the user's.

### Asking another role, and things you find

- **Something another role must do** (a henge API a consumer needs, a
  dashboard change): open an issue labelled with *their* role,
  `status:ready` and the repos, saying why, and mark your own blocked on
  it: `bin/lith issue new --title ... --label role:henge --label status:ready
  --blocking 31 ...`, then label #31 `status:blocked`. Do not edit their
  repos.
- **A problem outside your task:** open an issue with `status:ready` and
  no role; the coordinator triages it. Do not fix it in your PR.

### Plans

Planning is kept apart from coordination. The **planner** (a role in
`roster.toml`, always an interactive session with the user, never
headless) explores a goal with the user and files what they agree on as
a **plan issue** on the board: the plan template
(`.github/ISSUE_TEMPLATE/plan.md` in benchLith) with its goal, design,
phases, order, decisions and open questions, labelled `plan`,
`role:coordinator` and `status:ready`. The planner writes no code or
docs, opens no PRs, and files no `role:<role>` task issues.

The **coordinator** claims the plan issue, raises its open questions
with the user, then files one sub-issue per phase (`bin/lith issue new
--parent <plan>`, with `--blocked-by` for the order), each carrying the
plan's decisions, and comments the breakdown on the plan issue. It holds
the plan issue until every sub-issue is closed, then closes it. A task
agent that finds a plan's decision does not cover its case asks on its
own issue, as usual; it does not edit the plan.

### Launched agents

An agent started by `bin/lith launch` runs headless (`claude -p`), with
`gh` and `--no-verify` denied, and its workspace pinned to one branch by
`git config lith.branch`. The launcher has already claimed the issue and
will open every pull request, label and comment -- you must not. Instead,
when you finish, write `agents/<role>/runs/<N>/result.md` with one of:

```
outcome: pr

--- <repo>
title: <single-line PR title>
body:
Agent: <role>

Closes astrum-code/benchLith#<N>

<multi-paragraph body>

--- <another-repo>
title: ...
body:
...
```

Put anything for the user to decide under a `## For the user to decide`
heading in that PR's body -- the launcher copies it to the issue as a
comment, so the user sees it there and not only in the PR. A question
that blocks the work -- you cannot finish without the answer -- is still
`outcome: blocked`, not a PR carrying the question.

```
outcome: blocked

<the question for the user or another role>
```

```
outcome: failed

<what went wrong>
```

A **review** -- your issue is to review another issue's work (a PR at
risk >= 8, a templet: "Tiers", above) -- opens no PR, and is not a
question. Name the issue under review, then give the findings:

```
outcome: review
reviews: #31

<the findings>
```

A proving mission ends in neither a PR nor a review but in a **`report`**
(or, not yet done, a **`checkpoint`**): "Proving", below.

Then exit. On the next `bin/lith launch` or `bin/lith agents` the
launcher opens the PRs, posting any `For the user to decide` section to
the issue too (`pr`), posts the question and labels `status:blocked`
(`blocked`), posts the findings on the reviewed issue as a comment headed
`review: <role>`, links it from your review issue and closes that as done
(`review`; no PR, no `blocked` label), or releases the claim (`failed`).
`bin/lith board` marks the reviewed issue `review from <role>` until its
author's next run, which gets the review among the comments since its
last run, as it does a question's answer. Branches
you pushed are kept. A run the account's **usage limit** stops is none
of these: the launcher sees it in the log, keeps the claim and the
branch, and resumes you after the reset, with your commits and
uncommitted changes.

**Report your phase**, so a long silence reads as work, not as a stall.
The phase line is **progress**, read by the supervisor's stall rule.
**Liveness is the supervisor's**: on the fleet it heartbeats your claim
lease, never you, and an agent never polls or renews anything itself.
`bin/lith agents` measures that you are alive (your process tree's CPU,
your logs growing); you say what you are doing, with three habits:

1. **A status line at every phase change** (starting, coding, running
   tests, writing the result), on events, not on a timer: append one
   line, starting with the time, to `agents/<role>/runs/<N>/status`.
2. **Any step that may take more than about 10 minutes** declares how
   long you expect it to take, and writes its output to a log named in
   the line:
   `02:31 running the full suite, expect up to 90 min, log: <path>`.
3. **Commit your work in progress at each phase change**, on your
   assigned branch, staged by path, with the `Agent:` trailer. A local
   commit costs nothing, and a run stopped from outside -- the usage
   limit, a crash -- then loses less. Push, when you push, only that
   branch.

The stall margins: on the fleet, an alive run with no progress raises a
supervision alert after its declared expectation plus 5 minutes, or 10
minutes when it declared none. The **breakglass** (`bin/lith` without
memLith) keeps its file rules: quiet past the expectation (or 20 minutes
with none), with no CPU used and no log growing, you are shown as
stalled, and a claim goes stale after 72 hours.

**In a headless run, never end your turn while a background task is
still running: ending the turn ends the run.** The task is killed, no
result is written, and the launcher releases the claim as failed. Run
commands in the foreground. One that may take longer than a tool call
allows (about 10 minutes) runs in the background, appending `exit:
<code>` to its log when it ends, and you wait for it **in the
foreground**: repeated waits, each under 10 minutes, until the log says
`exit:`, with a status line while you wait. Only then write the result.
Where the repo's venv has **pytest-xdist** (`bin/lith which xdist` lists
it for that repo), run the fast form first (`pytest -n 8`, then the
failures on their own), and the serial full suite only when the issue
needs it. Otherwise, the serial run through `bin/lith test <repo>` is the
gate. Never install a package to get the parallel form.

## Proving

The **`proving` role** (plan #757; `roster.toml`) runs the long jobs: it
proves a **scenario** end to end in a fresh VM -- the full install and an
example from X to Z, say -- and **does not stop to fix**. Each failure it
works around, inside its VM, and writes up (what broke, the evidence, the
workaround), so the real fix is reviewed and planned separately and the run
is never slowed by it. It confirms what was written but never run for real.
It owns no repo and never pushes to one; its output is a findings file, a
report and the VM's state.

- **A mission is an issue** labelled `role:proving`. Its body has a
  **`## Scenario`** section: a `VM:` line naming the target, then numbered
  steps, each with a **pass condition** and a **time bound**:

  ```
  ## Scenario
  VM: noble
  1. Install the stack -- pass: `smud --version` prints -- within 20m
  2. Run the example, X to Z -- pass: its last line is `Z done` within 1h
  ```

  A mission without a whole scenario is refused at launch. It runs at the
  deep class (the budget guard throttles it like any run), and needs no
  templet: it proves what is built.
- **One instance per mission, each instance its own VM.** `proving` has
  `instances = 2`; `proving-2` takes the next mission. An instance's VM,
  lab directory and anything else it creates carry its name --
  `lab-<instance>-<issue>`, with its own `laylith lab --home` -- so two
  instances never collide. A mission touches no repo (its workspace is
  benchLith's alone), so instances never wait on each other's repos; the
  repos it installs sit at their `origin/main` tips in its workspace, and it
  records the commit of each.
- **A host cap on lab VMs, and the smoke.** Every mission holds a **lab
  slot** from its launch until its report (the VM outlives the run's
  checkpoints); the proving role may hold **`lith.lab.cap`** at once
  (`git config`, default 2), and a launch past it waits, unclaimed, until a
  slot frees. The cap counts proving VMs only, and the host's lab is meant
  to have room for **one more, the smoke's** (#730): a proving run **never
  takes, holds or waits for the smoke lock**, so a smoke never waits behind
  a long run. (Set the cap one below what the host's lab can hold.)
- **A long run.** The launcher resumes a mission after each usage-limit
  reset and after each **checkpoint** until the scenario passes or the
  mission is stopped. A checkpoint is `outcome: checkpoint` (the agent's
  context is full, or it is at a good place to resume), or a run that ends
  with no result at all: either keeps the claim and the VM, labels the
  issue `status:ready` and comments `checkpoint:`. The agent reads its own
  report, findings and VM on resume and goes on from the step its status
  line names. **Each status line names the step**: `11:20 step 3/7 start the
  cell: waiting for the ring`.
- **The findings file**, `agents/<instance>/runs/<N>/findings.md`: one
  `## F<n>: <title>` entry per finding, appended **as it happens**, with
  `step`, `broke`, `evidence`, `workaround` and a suggested `owner` role. The
  sweep lists each mission's unfiled entries in `judgement.json`; the
  coordinator files each as a triage issue and adds `- filed: #N` to the
  entry (until #711's `## Found` mechanism files them itself). A finding is
  never fixed in place and never a reason to stop.
- **The report**, `agents/<instance>/runs/<N>/report.md`: per step
  pass or fail with timings, the commit of every repo it installed, the
  findings by number. **A finished mission ends `outcome: report`**, a
  result with no PR, like `review`:

  ```
  outcome: report
  scenario: passed|stopped

  <the report, if report.md is empty>
  ```

  The launcher posts it on the mission issue as `report: <role>`, frees the
  VM's slot and closes the issue. `scenario: stopped` is for a mission the
  user or the coordinator stopped with a **`stop: <reason>`** comment on
  the issue: the sweep drops a `stop` file in the running agent's run folder,
  a resume says it, and the agent writes the report from what it has; a
  `stopped` report for a mission nobody stopped is left `attention`.
  `outcome: blocked` (a tool the host lacks, a decision) and `outcome:
  failed` (the mission is released and its VM freed) are as for any run.
- **The operational review** (plan #976) is a standing mission: apply a
  release to staging, rehearse the PR's named rollback, run the release's
  scenario, watch for the declared period, and end the report with
  `verdict: fit for production` or `verdict: not yet`.
- **Never**, as the roster says: edit or push to any repo; fix a finding
  in place; merge; install on the host (the lab's tools are the operator's,
  `laylith lab doctor` names them -- inside its VM it installs freely).

## Git: always a branch, always a pull request

`main` takes changes only through pull requests, and only the user merges
them, except rules-sync PRs, which the coordinator merges with
`bin/lith rules --merge` after the benchLith rules PR is merged, and green
**patch** lock-bump PRs (see "Versions and changelogs"), and the
sweep's own merges at risk 4 or below (see "The sweep"). A minor bump
is the user's to merge.

- **Branch before your first edit**, off an up-to-date `main`:
  `git fetch origin && git switch -c <type>/<short-name> origin/main`.
- **Never commit to `main` or push to it.** Never force-push a branch
  someone else may have, never rewrite pushed history, and never skip
  hooks (`--no-verify`) to get around a guard.
- **To bring a PR branch up to date with `main`, or to fix a conflict,
  merge `origin/main` into it** (`git fetch origin && git merge
  origin/main`). Never rebase a branch that is pushed.
- **Open a pull request when the work is done** (`gh pr create --base
  main`), and leave it for the user. Do not merge your own PRs.
- **Do not touch other agents' work.** If this checkout is on a branch
  you did not make, or has changes you did not make, another agent is
  using it: do not switch, stash, reset or commit it. Ask, or work in a
  separate clone or `git worktree`.

### Branch names

`<type>/<short-kebab-description>`, where the type is what the change is:

| Type | For |
|---|---|
| `feat/` | new behaviour |
| `fix/` | a bug fixed |
| `perf/` | the same behaviour, faster or smaller |
| `refactor/` | the same behaviour, better code |
| `test/` | tests only |
| `docs/` | documentation only |
| `chore/` | build, dependencies, tooling, housekeeping |

Work from a board issue leads with the issue number:
`feat/31-catalog-fields`. A step of a written plan may lead with its
number: `feat/8.4i-2-proportional-folds`. One branch is one topic:
unrelated work gets its own branch and PR.

### Commits

- **`<type>: <summary>`** on the first line, with the types above (and
  `build:` for packaging), in the present tense. The body says why, and
  what a reviewer would not see in the diff. The last lines are trailers:
  `Agent: <role>`, then any `Co-Authored-By`.
- **Stage paths by name** and read `git status` before each commit.
  `git add -A` sweeps in whatever is lying around.
- **Never commit** secrets or keys (`*.key`, `key_secret`, `.env`, `*.pem`,
  tokens), virtual environments, `__pycache__`, runtime data or logs. If
  something like that shows as untracked, it belongs in `.gitignore`.

### Pull requests

- **One PR per repo.** A change that spans repos is one PR in each, with
  the same branch name. Each PR lists the others and the order to merge
  them in.
- **The description says** who you are (`Agent: <role>`), the issue it
  closes or is part of, what changed and why, what was tested (and how),
  what was **not** tested, and anything the user has to decide.
- **Before opening it**, run this repo's tests and linters (`black`, and
  `flake8` with an 88-column limit) and say in the PR what passed. A PR
  that changes a package's `src/` also passes `bin/lith version check`
  (see "Versions and changelogs").
- **Once your PR merges, its branch is done.** Do not push to it again:
  follow-up work starts a new branch off `main`. Merged branches are
  deleted from GitHub (benchLith's `refresh-mains`) -- safely, because a
  branch pushed to after its merge is kept, and a merged PR can restore
  its branch.

## Versions and changelogs

Every package follows one rule (decided by the user, 2026-10-13; the
version line changed 2026-10-18): **while it is 0.x, a patch for fixes and
docs, a minor for any API addition or change -- and the version line moves
only at release.** The version lives in `pyproject.toml`. A PR says *how
much* it moves the next release, in a changelog fragment; it never edits
the number. (Every PR used to raise it, so each merge left the next PR
conflicting in `pyproject.toml` and cost a resync and a re-review.)
Packages that share a protocol move together: when a change to one needs
the others to change (a wire format, a record schema), each adds its own
fragment, in the merge order the PRs name and released in that order. A
package's first version is 0.2.0 (those at 0.1.0 move there with their
first release).

- **One changelog fragment per PR**, never an edit to `CHANGELOG.md` or to
  the version line (editing either made every merge conflict with the
  repo's other open PRs). A PR that changes a package's `src/` adds
  **`changes/<issue>.md`** in the package (next to its `pyproject.toml`; a
  PR with no board issue names it by its branch). Its front line is
  `bump: patch` or `bump: minor`, by the rule above; then one line, saying
  what changed for a user of the package, not how:

  ```
  bump: patch
  - `Client.write` retries a dropped connection once
  ```

  Two PRs each add their own fragment, so they never touch the same lines.
- **A release sets the version and folds the fragments**: `bin/lith
  changelog fold <repo>` (run by `bin/lith release <repo> --prepare`)
  computes the release's version from the package's pending fragments --
  **any `minor` makes a minor, else a patch** -- sets it in
  `pyproject.toml`, moves every `changes/*.md` into `CHANGELOG.md` under
  its `## <version>` heading (newest first) and deletes them. It is a
  `chore:` PR of its own, and the only PR that moves the version line.
- **`no-bump`**: a change to `src/` that is not a change to the package (a
  comment, a rename of a private name, a test helper kept in `src/`) adds
  no fragment. The PR carries the `no-bump` label, **with a reason** in its
  description (`bin/lith version check --no-bump "<reason>"`). A bare
  label, with no reason, fails the check.
- **`bin/lith version check`** is the gate for this: for each package whose
  `src/` the diff changes against `origin/main`, the PR adds a fragment
  with a `bump:` line and an entry (or `--no-bump` gives a reason), and
  leaves the version line alone. It exits 1 otherwise. (Transition,
  user, 2026-10-19: until 2026-10-26, a week after this rule merged, a PR
  that raises the version line draws a **warning**, "the version moves
  only at release: drop the line, keep the fragment with `bump:`", not a
  failure; from that day it fails. The day is `VERSION_LINE_FAILS_FROM`
  in `bin/lith`.)
- **Locks stay pinned until a bump PR vets the new version.** A dependent's
  `poetry.lock` moves only in a `chore/` bump PR (`bin/lith bump`), whose
  gate runs the dependent's tests against the new version; **a PR that is
  not a `chore/` one does not touch `poetry.lock`** (the review checklist
  fails it). `bin/lith locks` reads the changelog fragments added (folded
  since or not) between the pinned commit and `main`, and the version
  line, and says what each pin's move is: **a patch** (bump freely),
  **a minor** (read the changelog before bumping), or **commits with no
  fragment** (the package changed `src/` without a fragment or a new
  version: flagged, for its owner).
- **Who merges.** The coordinator may merge a **green patch** bump PR: a
  `bin/lith bump` lock PR that moves pins by patches only, once its gate
  is green, unless its issue is scored `risk:8` or more ("Tiers: the
  scores"). A **minor** bump is the user's to merge, as is every other
  PR, a package's own change included -- except what **the sweep** merges
  (plan #780): a pool-approved PR at **risk 4 or below**
  (`SCORE_POLICY["auto_merge_risk"]`; "The sweep", above), whose gate is
  green and whose questions are answered; a set of PRs only whole and in its
  stated order. Risk 5 and above, anything a review flagged, anything
  `overruled` and every templet PR stay the user's. The outcomes view counts
  the sweep's merges and the reverts of them; after two weeks the figures
  decide whether risk 5 joins. To switch the rule off, set
  `auto_merge_risk = 0`.
- **Plan status is the board's, not PLAN.md's.** A repo's `PLAN.md` keeps
  prose and links -- the design, the decisions, the issues -- and carries
  no per-phase status, which every PR would otherwise edit and conflict
  on. `bin/lith plan status <repo>` renders what is ready, claimed,
  blocked or in review from the board.

## Real before fake

Code that runs a command on a host -- a bootstrap or apply step, a unit,
a sudo call, an overlay command, a cloud-init fragment -- **is not done
until it ran in the lab** (user, 2026-10-17). A fake transport is for
parsing, rendering and planning only. The rule holds for layLith and for
MeshLith's host-touching paths (the overlay drivers, the units, the
restart step, the agent's install).

A suite sorts its tests into three classes, and the suite says which is
which:

| Class | Marker | Fakes | For |
|---|---|---|---|
| unit | none | allowed | documents, rendering (units, cloud-init, Caddyfiles, sudoers lines), `plan`/`diff` logic, argument parsing |
| container | `@pytest.mark.lab("container")`, in `tests/test_<thing>_container.py` | none | a container with `systemd` as PID 1 and a real `sshd`, the real `Ssh` transport: packages, users, keys, sudoers, units written and started, `plan`/`diff`/`apply` round trips. Minutes |
| vm | `@pytest.mark.lab("vm")`, in `tests/test_<thing>_vm.py` | none | a Vagrant+QEMU VM from a vendor cloud image: ZFS, kernel WireGuard, cloud-init, the reboot, bootstrap end to end. Tens of minutes |

- **`bin/lith test <repo>` runs unit and container** where the repo's
  pyproject registers the `lab` marker, as two steps (`unit`,
  `container`); a container class that skipped every test (no engine
  answers) shows as skipped, never green. The vm class is not in it.
- **A PR that touches host-touching code names its lab report.** The
  gate per PR is the smoke, `laylith lab smoke`: one VM cell (noble,
  arm64 on the laptop; bound 15 minutes, past it the PR says why), which
  writes `lab-report.json` with the head commit's hash and the cell's
  figures. The PR body carries the line
  `Lab report: <path to lab-report.json>`; a change that only renames or
  documents says `Lab report: n/a -- <reason>`, which the reviewer
  judges. Until a pipeline runner has HVF or KVM the smoke runs on the
  laptop, in the author's workspace, one VM at a time.
- **`bin/lith lab check <repo> [--pr N]` verifies it:** the body names a
  `lab-report.json`, its commit is the PR head (so a push after the smoke
  needs a new smoke), its cells are green. The reviewer refuses a PR
  without a current report; merge-ready shows the report beside the PR.
  `LAB_HOST_PATHS` in `bin/lith` lists, per repo, what counts as
  host-touching.
- **Tools the lab needs are never installed by an agent:** `laylith lab
  doctor` prints the install lines for the operator. A lab test whose
  tools are missing is skipped with the reason, never passed.

## Scale tests

Scale, resilience and elasticity tests (plan #263; user, 2026-10-13) are
**checked-in profiles run by pipelines**. Agents write profiles and fix
code; they **never drive a run and never improvise one** -- no ad hoc
cluster, no one-off script that stands in for a profile, no run by hand to
produce a figure for a PR. A figure that matters comes from a profile's
run, on the record the pipeline keeps.

- **Tiers.** Tier 0 is local, small and free: the profiles a package ships
  that run on one machine in well under a minute. Tiers 1-4 run on
  DigitalOcean droplets by a pipeline, under their monthly budgets, and
  are never started by an agent.
- **`bin/lith test` runs tier 0**, one step per profile (`tier0-<profile>`,
  at a fixed seed), after the repo's tests and before its lint, and a
  profile that breaks an invariant fails the repo like a failing test.
  `--no-tier0` leaves them out of a quick local run, not out of the gate a
  PR is judged by. `TIER0_PROFILES` in `bin/lith` lists, per package, which
  of its profiles are tier 0.
- **A new profile is a PR like any code**: the profile file in its package,
  and a line in `TIER0_PROFILES` if it is tier 0 (small, one machine, no
  droplets). A profile that needs droplets, or takes minutes, is tier 1 or
  up and is run by a pipeline.

## Docs that pin code

Architecture docs -- a flow chart per feature (Mermaid in Markdown) and the
wire datasets as tables -- say which code they describe, so a change to
that code shows which pages it makes wrong. Such a page opens with front
matter:

```yaml
---
title: The write
owner: henge              # the role that owns the mechanism
mechanism: write          # what the page is about; groups the atlas
reviewed: vistalith       # optional: the other role that reviewed it once
pins:
  - repo: henge-core      # a repo's directory in benchLith, or its GitHub name
    paths: [henge/core/ring.py, henge/core/wire/]   # files, directories, globs
    at: 3f2a9c1           # a commit of that repo the page was right at
  - repo: henge-store-client
    symbols: [henge/store/client.py::Client.write]  # path::Name (Python:
    at: 8d41e07           #   Class.method), changed when its source differs
---
```

Only these keys (and `kind`, below), and a small YAML: `key: value`, `- `
lists, `[a, b]` and `{k: v}`. A page needs `owner` (a role in
`roster.toml`), `mechanism` and at least one pin; a pin needs a `repo`,
`paths` and/or `symbols`, and `at`.

A **conceptual page** -- one about an idea, not a piece of code -- says so
with `kind: concept` and `pins: []`. It still needs `owner` and
`mechanism` and appears in the atlas, but pins nothing, so no code change
unpins it and it never goes stale.

- **A doc that pins code is updated in the PR that changes it**: edit the
  page, and set each pin's `at` to the commit of the code it now matches.
  A PR beside another repo's code PR names it, as for any cross-repo change.
- **Or the PR carries `docs-later`** (a label, on the PR and on its
  issue). It may stand **one week on main**; after that the gate fails
  until the page is updated.
- `bin/lith docs check` lists the pages a diff unpins (against
  `origin/main` in each repo; `--docs-later` allows them) and the pages
  whose pinned code changed on main since their `at`, failing those past
  the week. `bin/lith docs index` writes `docs/ATLAS.md` in benchLith,
  every page by mechanism and repo with its owner and freshness; run it,
  and commit the result, when a page is added or changes.
- The role that owns the mechanism owns its page; another role reviews it
  once (`reviewed:`). A repo that is, or may become, public carries the
  front matter (it names roles and other repos) only until the mirror
  filter strips it: see "Public and private".

## Templets

A **templet** is the architecture spec of deep work, agreed *before* the
work is cut: interfaces, invariants, acceptance, boundaries and rationale
-- what to build and why, not the code. A worker builds to a design that
someone with context has read, and once built the templet stays as the
atlas page of the mechanism.

- **When** (the score table, "Tiers: the scores"; `SCORE_POLICY` in
  `bin/lith`): required at **tier >= 4 or risk >= 7**; optional at tier 3,
  when the coordinator wants the design agreed first; never at tier 1-2.
  A second role reviews it at tier 5 or risk >= 8, and at risk >= 8 the
  user approves it.
- **Who drafts it, who reviews it:** the **templet role** (`templet` in
  `roster.toml`: four instances, always the deep class) drafts it, in a
  spec-only run (`bin/lith launch --spec N`, an issue whose result is the
  templet, not code) -- not the role that owns the repo. It owns no repo
  and touches nothing outside `docs/templets/`. When its PR opens, the
  launcher files the **owning role's review** of it (`outcome: review`,
  `reviews: #N`, a `kind: templet` line in the review issue): is it
  feasible against the code, and does it fit the repo? A templet the owner
  finds infeasible (`verdict: changes`) goes back to the templet role with
  the findings, to redraft on the same branch; **the owner never edits a
  templet**. The templet role also reviews the built PR for conformance to
  the page, beside the pool's code review (see "Review", above), and keeps
  benchLith's `docs/templets/README.md` (the skeleton) current.
- **Where:** `docs/templets/<issue>-<name>.md`, in the repo that owns the
  mechanism (benchLith's `docs/templets/README.md` has a skeleton). It is a page that pins code
  ("Docs that pin code", above) with two more front matter keys, `templet`
  (the number of the issue it is the spec of; the file name leads with it)
  and `status`:

  ```yaml
  ---
  title: The catalog fields
  owner: henge
  mechanism: catalog
  templet: 31
  status: agreed        # draft -> agreed -> built
  ---
  ```

  Its body has the headings `## Interfaces`, `## Invariants`,
  `## Acceptance` (each item a test), `## Boundaries` (what the work must
  not touch), `## Rationale` (why this design, and what was rejected) and
  `## History` (dated lines, one for the agreement and one for every
  approved change, each citing what approved it).
- **Small steps** (user, 2026-10-25): a templet also cuts its build into
  `## Build steps`, each finished by one PR that merges on its own, and
  names the code it builds on under `## Reuse` (both recommended, not
  checked). The coordinator files one sub-issue per step (`--parent` the
  templet's issue, `--blocked-by` for the order). The conformance review
  of a step's PR checks it against that step's acceptance items.
- **`status`:** `draft` while it is written (no pins, no headings
  required, not attached); `agreed` once the user has merged it: **the
  templet's own PR carries `status: agreed`, and the user's merge of that
  PR is the agreement** -- no separate comment, no follow-up PR. Its
  History line cites the merge
  (`- 2026-10-17 agreed (merge of this PR, user)`). The owning role's
  review, and where a second role also reviews the templet (tier 5 or
  risk >= 8) its review, come before the merge; the user reads them with
  the PR. `built` when the PR that builds it
  sets it, pinning the code as any page does -- from then on it is kept
  current like any atlas page. `bin/lith docs check` reads the keys and
  the headings.
- **Attached:** a templet is attached to issue N when a templet with
  `templet: N` and `status: agreed` (or `built`) is on its repo's main.
  `bin/lith launch` refuses an issue at tier >= 4 or risk >= 7 without
  one. A comment on the issue that says **Templet: not required** waives
  it (the issues cut before the rule took effect), as **Templet: this
  issue writes it** does for the issue that writes the templet.
- **The worker's duties** (a launched agent's instructions repeat them):
  read the templet first and build to it -- its interfaces, its
  invariants, within its boundaries. Commit the acceptance tests **first**,
  one per `## Acceptance` item, each carrying the marker `templet:<issue>`
  (a comment, a docstring or a marker's argument in a test file of the
  templet's repo), see them fail, then build until they pass. The PR that
  finishes sets `status: built` and the pins. `bin/lith test` fails a
  built templet with no test that carries its marker; the repo's suite
  says whether those tests pass.
- **An agreed templet is not the worker's to change on its own.** If it is
  wrong, or does not cover the case, the worker may **propose the change in
  its PR**: the page edit and a History line `- <date> proposed: <what>
  (this PR)`. The conformance review (see "Review") answers each proposal
  with `page: approved` or `page: refused -- <why>`. On approve, the change
  is agreed when the PR merges and its History line reads `- <date>
  changed: <what> (approved: the conformance review of <PR>)`; on refuse
  the finding is must-fix and the author is resumed. **At risk >= 8 the
  proposal still waits for the user**: the review is only an opinion, and
  the user's merge of the PR agrees it. A change that needs more than a
  PR can carry, or comes after the merge, is said on the issue, with why
  (`outcome: blocked` when headless), and waits for a **comment the
  coordinator approves** (the user, at risk >= 8); the change is then made
  in the page, with a History line that cites that comment (`- 2026-10-18
  changed: <what> (approved: <comment URL>)`). `bin/lith docs check`
  accepts both forms: a `proposed:` entry must end `(this PR)`, a
  `changed:` entry must cite what approved it.

## Public and private

Some repos are mirrored to a public org (open core; the split, the
licences and the mirror flow are in benchLith's `docs/PUBLIC.md`). So in
any repo that is, or may become, public:

- **Never put internal names, hosts, paths, plans or agent files where a
  public repo will carry them**: no other private repo's name, no role
  names, no `AGENTS.md`, `CLAUDE.md`, `roster.toml`, `.claude/`,
  `agents/` or plan files, no tokens or keys. If unsure whether a repo or
  a path is public, treat it as private and ask.
- **A private capability is a private plugin package** that registers
  into a public extension point -- never an extra, a flag or a guarded
  import in a public package. It is resolved from a private index on the
  mesh. What the extension point lacks is an issue for the role that
  owns the public repo.
- **Nothing is edited in a mirror by hand.** The public side is produced
  from the private repos by the filter, lint and scan, through a PR the
  user merges.
- **Only the user lands a mirror PR** (`bin/lith polish --land`) or makes
  a `public-mirror` branch (`--bootstrap`); no agent, the coordinator
  included, runs either. Nor does an agent acknowledge a secrets finding
  on its own: a `[secrets]` line records the user's review.

## Secrets

Decided by the user, 2026-10-14, for every repo: **a secret -- a key, a
token, a password, a passphrase -- is never passed through a command
argument or an environment variable.** Arguments show in `ps` and shell
history, and the environment is inherited by every child process, shown in
`/proc` and written to crash dumps and logs.

- **A secret lives in a file with mode `0600`, in a folder with mode
  `0700`.** Create it with that mode from the start (`os.open(path,
  os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)`, or write a temporary
  file in the same `0700` folder and rename it), never write it with the
  default mode and `chmod` after: another user could read it in between.
  A secret written to any file without `0600` is a defect.
- **The path is what travels:** by argument or a setting (`--key-file
  PATH`), and/or found at a default location (a `~/.config/<tool>/` or
  state folder). The code that reads it refuses a file that is group- or
  world-readable, naming the path and the mode, the way `ssh` does.
- **Nor is a secret in a URL, a log line, an error message or a committed
  file.** A command that must hand a secret to a child process passes an
  open file descriptor or the file's path, never the value. Public
  material (a public key, a key id, a fingerprint) is not a secret.
- **Exception (user, 2026-10-14):** memLith's yard may reference a
  provider key as `env:` **for local development only**; that is refused
  once anything binds beyond localhost or syncs to a hosted tenant
  (benchLith#348). It does not cover an agent's own API key: that comes
  only from a `0600` key file.
- **Exception (user, 2026-10-24), a migration fallback:** an agent's
  memLith key comes from `key_dir/<role>.key` first, and the launcher's
  own `MEMLITH_API_KEY` is used only when that file is missing
  (benchLith#352). It stays until the key files are everywhere; the
  launcher then hands the agent a `0600` file, never the variable.
  Everywhere else the rule stands.
- **Found one?** Do not fix it in an unrelated PR: it is an issue for the
  owning role (`docs/audits/secrets-in-env-and-args.md` lists the known
  ones).

## The machine

Agents share the host with the user and each other. Never install system
or global packages (`brew`, `apt`, `npm -g`, `pipx`, `sudo` and the like)
and never change anything outside your workspace and the repo's own venv:
one `brew install node` once upgraded openssl and changed the host's
default. Per-repo venvs (`poetry install`) are fine. If a tool you need
is missing, stop and ask (headless: end with `outcome: blocked`).

**Never read, list or search outside your workspace, the repo's own venv
and your run folder.** No `find`, `bfs`, `fd`, `rg`, `grep -r`, `du`,
`ls -R`, `mdfind` or `locate` over `/`, `~`, `/Users`, `/Library`,
`/System`, `/Volumes`, `/opt`, `/usr` or `/private`: a walk of the host
reads other people's directory names and can raise macOS privacy prompts
(two agents did it, looking for a file and for `black`). A file, tool or
module that is not in the workspace is `outcome: blocked`, saying what is
missing. `bin/lith which <tool|module>` answers from the workspace's venvs.

Exception (user, 2026-10-25, benchLith#2294): launched agents may read
`~/benchlith-yard/.memlith/` for their MCP config and memLith's socket;
writes there go only through memLith itself.

## Dependencies between the repos

Each repo depends on its siblings as git dependencies on their `main`
(for example `henge-core = { git = "...", branch = "main" }`), pinned to
a commit by `poetry.lock`.

- **A change a sibling needs** goes in the sibling's own repo, as its own
  PR. Say in both PRs which must merge first.
- **After a sibling's PR merges, refresh the lock** where you need it,
  as its own PR: `poetry update <package> ...` on a `chore/` branch.
- **Never point a dependency at a local path** in a committed
  `pyproject.toml`. Locally, benchLith's `bin/dev-link.sh` swaps in the
  local trees without touching the files.

## CI/CD: not on GitHub

GitHub is the off-site backup, not where pipelines run. CI/CD runs on
MeshLith, on machines we own (at home, later perhaps a cloud host); see
benchLith's `meshlith/docs/CICD.md`.

- **Do not add GitHub Actions workflows** (`.github/workflows/`), and do
  not add tokens or secrets to make one work.
- **A repo's pipeline is its gate**: `.meshlith/pipeline.yaml` in the
  repo, MeshLith's pipeline document, run by the mesh at each commit and
  reported as a commit status. What it says and how to write one is in
  benchLith's `docs/PIPELINE.md`. A repo with a pipeline changes it like
  any code, in a PR, and runs `bin/lith pipeline check <repo>` first. A
  changed pipeline is not run by the mesh until the user has approved its
  new content, so say in the PR that it needs approving.
- **`bin/lith test` stays the local gate**, and the pipeline runs the same
  checks: run it yourself before you open a PR, and say in the PR what
  passed. A green pipeline does not replace that, and a repo with no
  pipeline yet has only it.

## Decisions

When a choice is the user's -- a trade-off, a behaviour change, anything
hard to undo -- ask before building it, and record the answer where the
work is planned (a `PLAN.md`, or `.claude/plans/`). If you find a problem
outside your task, report it, or note it in the plan; do not fix it in the
same PR.
