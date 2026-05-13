# AGENTS.md — Orientation for AI Assistants

Read this first if you're an LLM helping with this repo. It is the canonical
brief; everything else (READMEs, doc strings, skill bodies) is reference
material under it.

## What this project is

**o7k-expediter** is an automated pipeline that takes new upstream OpenStack
releases and walks them through Debian packaging for Ubuntu and the Ubuntu
Cloud Archive: release detection → packaging → build/test → Rock (OCI image)
rebuild → charm update → merge request. The goal is "touchless packaging" —
supported OpenStack releases flow into Ubuntu with minimal human intervention.

It sits on top of **Packastack** (https://github.com/MylesJP/packastack), the
deterministic packaging tool with an existing AI-assisted `build-doctor`
branch. o7k-expediter generalizes that AI-assist pattern to the whole
pipeline.

Repo: https://github.com/MylesJP/o7k-expediter

## Architecture in one paragraph

A **deterministic expediter** sits at the top and runs a state machine — it
never calls an LLM. It dispatches work to **AI-driven managers** (packager,
rockcrafter, charmer), one per pipeline stage. Each manager runs a bounded
iteration loop: invoke a deterministic tool (Packastack, rockcraft, etc.), and
on failure dispatch a **skill** to propose a fix. Skills are markdown files
with YAML frontmatter — content, not code. A skill returns a structured
response (usually a proposed patch), the manager validates it against the
skill's declared scope, applies it as a signed git commit, and loops until the
stage passes its gate criteria or the iteration budget is exhausted. When a
manager gets stuck, the expediter generates an intervention bundle for a human.

## Hard invariants (do not violate without re-opening the design)

1. `o7k/expediter/` **never imports** `o7k/llm.py`. The expediter is
   deterministic.
2. `workpackage.yaml` is the **single source of truth** for run state. Logs
   and provenance live in git history, not separate files.
3. Every state change is **exactly one signed commit**. No bundled,
   multi-purpose commits.
4. Stamps require **both** the YAML entry and a verifiable git signature on
   the referenced commit.
5. Managers **don't modify packaging files directly**. They apply
   skill-proposed patches to `repo/` after validation. (Managers do write to
   `workpackage.yaml` — that's their job; see invariant 8.)
6. Skills declare their scope in frontmatter (`allowed_modifications` /
   `forbidden_modifications`). The runner enforces this **after parsing the
   response, before applying anything**.
7. A new skill should not require Python changes unless it needs a genuinely
   new output format or context type. Most additions are pure config.
8. **Only managers update `current_stage` and `current_owner`.** Skills
   never touch these fields. The runner never touches them. The expediter
   never touches them. State advance is exclusively the manager's act after
   reading the stage's stamp and applying gate policy.
9. **Skills are observers; managers are deciders.** Skills report findings
   into stamps (facts, comparisons, proposed patches). Managers read stamps,
   apply policy from `resources/gates.yaml`, and decide whether to advance,
   retry, or escalate.

## The work package

State for a single pipeline run lives in **one git repo per run**, containing:

- `workpackage.yaml` — identity, lifecycle status, target info, inputs, stamps
  (one per completed stage), append-only history log, open issues, and an
  intervention block (populated only when escalated).
- `repo/` — the cloned Debian packaging repository as a subtree.

`workpackage.yaml` is the **shared state across every AI agent in the run**.
No agent talks to another directly; they read and write through this file.
A fresh agent invocation can fully reconstruct the state of play by reading
it.

### Lifecycle

1. The **expediter** creates the work package from `resources/wp_template.yaml`
   (which hardcodes the Hibiscus dev-cycle scope), commits it, and dispatches
   the manager for `current_stage`.
2. The **manager** dispatches the stage's skill with the env contract above.
   The skill emits structured output — it never edits files directly.
3. The **runner** translates the skill's output into a single signed commit
   on `workpackage.yaml`: it persists any discovered values into `target.*`,
   appends a stamp to `stamps:`, and appends an entry to `history:`. The
   runner does **not** touch `current_stage` or `current_owner`.
4. The **manager** reads the new stamp, applies the gate policy from
   `resources/gates.yaml`, and chooses: **advance** (write a new
   `current_stage` and `current_owner` to the workpackage, signed commit),
   **retry** the stage, or **escalate**. Stamp results that the gate policy
   reasons over are `verified`, `verified_with_warnings`, `rejected`, or
   `needs_human_review`.
5. The expediter loops back, reads the (possibly updated) `current_stage`,
   and dispatches the next manager — or terminates the run if `status`
   reached a terminal value. Example: detection-stage manager reads a stamp
   with `STATE: UP_TO_DATE`, sets `status: terminated`, and never advances
   `current_stage`; the expediter sees `terminated` and stops.

When a manager escalates, it writes `status: escalated`, populates
`intervention:` in the YAML, and the expediter creates an `intervention/<id>`
branch and parks the run. The human clones the work-package repo, pushes
commits back, and the expediter resolves clean-resume / auto-rebase /
conflict.

### How skills produce state changes

Skills don't reach into the workpackage YAML. They emit structured output per
their `output_contract`:

- **`patch`** — a unified diff. The runner validates against
  `allowed_modifications` and commits it to the `repo/` subtree on an
  `attempt/<stage>/<n>` branch.
- **`routing`** — an observation block (e.g. detector's
  `STATE: NEW_RELEASE / NEW_PRERELEASE / UP_TO_DATE / IN_FLIGHT / UNCERTAIN`)
  plus the facts that led to it. The runner persists the carried fields into
  `target.*` and writes a stamp. **The runner does not advance state.** The
  manager reads the stamp, applies gate policy, and writes the new
  `current_stage` if it decides to advance.
- **`report`** — a human-readable explanation. The runner writes a stamp with
  result `needs_human_review`. The manager (not the runner) populates
  `intervention:` and sets `status: escalated`.

The detector skill is the reference example of a `routing` contract: it
reports the upstream-vs-packaged state plus the discovered version/source
values. The runner stamps the workpackage with that finding; the manager
reads the stamp and decides whether to proceed to packaging.

## Skills (the main extension point)

A skill is one directory under `skills/<name>/` containing at minimum a
`SKILL.md`. Frontmatter fields:

```yaml
name: <unique identifier>
description: <one-line summary>
version: <int, bump on prompt changes>
output_contract: <patch | report | routing>   # which parser handles the response
when_to_use: <free text for routing>
requires_context:                              # context blocks to assemble
  - failure_header
  - sbuild_log_tail
allowed_modifications:                         # globs the skill may modify
  - "debian/patches/*"
forbidden_modifications:                       # explicit denylist
  - "debian/rules"
  - "debian/control"
pre_context_scripts:                           # run deterministically pre-LLM
  - fetch_something.py
model_tier: <cheap | heavy>                    # OpenRouter routing
confidence_floor: 0.6
handles_failure_classes:
  - <failure-class-name>
```

The body is the prompt sent to the LLM. The reference output shape for a
patch-producing skill is the structured block used by `build-patch`
(`DIAGNOSIS:` / `ACTION:` / `EXPLANATION:` / `PATCH_FILENAME:` / `--- BEGIN
PATCH ---` / `--- END PATCH ---`). New skills should mimic that style.

**Pre-context scripts** run before the LLM call. Their stdout is injected into
the prompt as a named context block. The LLM never executes scripts — it only
reasons over text.

### Skill invocation env contract

When a manager (or the expediter) dispatches a skill, the runner executes each
`pre_context_script` with a defined env derived from the work package's
`target:` block. This is the single contract — scripts may rely on these names
existing:

| env var            | source                              |
|--------------------|-------------------------------------|
| `PACKAGE`          | `target.upstream_project`           |
| `OPENSTACK_SERIES` | `target.openstack_series`           |
| `UBUNTU_SERIES`    | `target.ubuntu_release`             |
| `UCA_POCKET`       | `target.uca_pocket`                 |
| `WORKPACKAGE_DIR`  | path to the work-package repo       |

Each script emits a `=== <block_name> ===` header followed by key/value lines,
which the runner injects into the prompt under that block name. The detector
skill is the reference implementation.

Adding a new skill: `cp -r resources/templates/skill skills/my-new-thing`,
edit `SKILL.md`. No Python changes unless a genuinely new output contract or
context type is needed.

## Managers (the iteration loop)

```
read workpackage state
while iteration_budget remaining (default 5):
    invoke deterministic tool (Packastack / rockcraft / ...)
    if success:
        run qa-validator skill
        if gates pass: write stamp, commit, hand off to expediter, return
        else: escalate, return
    else:
        classify the failure → failure_class
        if same failure_class as previous → oscillation → escalate
        look up skill in resources/routing.yaml
        dispatch skill with relevant context
        validate proposed patch against skill's allowed_modifications
        if valid: apply as commit on attempt/<stage>/<n> branch, loop
        if invalid or NO_PATCH: escalate
escalate (budget exhausted)
```

Managers reason about **routing and progress**; skills reason about **the
fix**. Managers never touch files directly.

## Repo layout

```
o7k-expediter/
├── README.md
├── AGENTS.md                         # ← you are here
├── pyproject.toml
├── .env.example                      # OPENROUTER_API_KEY, GPG_KEY_ID
│
├── o7k/                              # all Python
│   ├── cli.py                        # `o7k run <pkg>`, `o7k resume`, `o7k inspect`
│   ├── workpackage.py                # load/save YAML + git operations
│   ├── skills.py                     # load SKILL.md, run via OpenRouter, parse
│   ├── llm.py                        # OpenRouter API call
│   ├── expediter/                    # deterministic state machine — NO AI
│   ├── packager/                     # packaging stage (manager + tool wrappers)
│   ├── rockcrafter/                  # rock stage
│   └── charmer/                      # charm stage
│
├── skills/                           # one dir each, content not code
│   ├── detector/
│   ├── build-patch/
│   ├── patch-fuzz-resolver/
│   ├── missing-build-dep/
│   ├── qa-validator/
│   ├── build-failure-triager/
│   └── escalation-writer/
│
├── resources/                        # data, not logic
│   ├── projects.yaml                 # tracked OpenStack packages
│   ├── gates.yaml                    # gate policies per stage
│   ├── routing.yaml                  # failure_class → skill
│   ├── models.yaml                   # cheap/heavy → OpenRouter model IDs
│   ├── wp_template.yaml              # workpackage.yaml template
│   └── templates/skill/              # cp -r to start a new skill
│
├── tests/
└── docs/architecture.md
```

## Tech stack

- **Python** for `o7k/`. **pydantic** for all structured data (workpackage
  schema, skill frontmatter, gate policies).
- **Sync, not async** — pipeline isn't throughput-limited.
- **PyYAML** for YAML I/O.
- **git** via direct subprocess calls or GitPython — pick one and stick with
  it.
- **OpenRouter** for LLM calls. Model IDs live in `resources/models.yaml`,
  not in code. Two tiers: `cheap` (start with `anthropic/claude-haiku-4-5`),
  `heavy` (`anthropic/claude-sonnet-4-5` or similar).
- **Packastack** invoked as subprocess. Its `skill-doctor` branch returns
  structured failure output worth leaning on.
- **No async, no web framework, no DB.** State is files + git.

## How it runs today

Manual only. CLI is the trigger:

```bash
o7k run nova --version 31.0.1 --release jammy
```

This reads `resources/projects.yaml`, creates a new work-package git repo,
writes `workpackage.yaml`, clones the packaging repo into `repo/`, and hands
off to the expediter. Eventually a GitHub Actions cron will run
`o7k detect-and-run --all`. **Don't build the cron yet.**

## Build order

Each step produces something demonstrable.

1. `resources/projects.yaml` schema + one entry (cinder is the current pilot).
2. `resources/wp_template.yaml` — canonical state shape. Hand-write one to
   test against before any Python.
3. `o7k/workpackage.py` — load/save/commit primitives. Round-trip a
   workpackage through git.
4. `resources/gates.yaml` + gate evaluation in `o7k/expediter/expediter.py` —
   pure functions, easy to test.
5. `o7k/packager/packastack.py` — drive Packastack from Python, capture
   structured failures. Still no AI.
6. `o7k/expediter/expediter.py` happy path — runs Packastack, writes a stamp
   on success, escalates on failure. End-to-end pipeline with zero AI.
7. `o7k/skills.py` + `o7k/llm.py` + drop in the first `build-patch` skill.
   Verifies the skill machinery end-to-end.
8. `o7k/packager/manager.py` — wire the iteration loop with one skill.
9. Escalation/resume in the expediter.
10. Additional skills, then rockcrafter, then charmer.

**Principle:** deterministic skeleton first, then add intelligence at one
well-defined point at a time.

## Current state (snapshot — verify with `ls` before relying on it)

The repo has very little so far:

- `README.md` (stub)
- `resources/projects.yaml` — one entry (`cinder`)
- `resources/wp_template.yaml`
- `skills/detector/` — first skill (release-detection), with pre-context
  helper scripts; runner that consumes it does not yet exist

Most of the layout above is **not yet built**. The first real Python is build
step 3 (`o7k/workpackage.py`).

## Things to avoid

- **Don't over-engineer.** One person can hold this in their head. Resist
  class hierarchies, base classes, framework abstractions, premature splits.
  Files split when they cross ~600 lines or contributors actually conflict,
  not before.
- **Don't put logic in the expediter.** When in doubt, the expediter does
  less.
- **Don't make skills smarter than they need to be.** A skill does one thing
  within a bounded scope. Cross-cutting reasoning belongs in the manager.
- **Don't add Python where YAML works.** Routing maps, model tiers, gate
  policies, package lists — all data, all in `resources/`.
- **Don't build the cron / dashboard yet.** Out of scope.
- **Stable OpenStack releases are out of scope** for now. Only the current
  dev cycle (`openstack_series` in `resources/projects.yaml`, presently
  `hibiscus`) is tracked. No backports.

## Reference

- Packastack: https://github.com/MylesJP/packastack — read the `skill-doctor`
  branch for the existing AI-assisted skill pattern that this project
  generalizes.
- OpenStack releases (upstream version source of truth):
  https://opendev.org/openstack/releases/src/branch/master/deliverables
- Ubuntu OpenStack Dev packaging repos (downstream packaging source of truth):
  https://code.launchpad.net/~ubuntu-openstack-dev/+git
