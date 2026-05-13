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
5. Managers **don't modify files directly**. They apply skill-proposed patches
   after validation.
6. Skills declare their scope in frontmatter (`allowed_modifications` /
   `forbidden_modifications`). The runner enforces this **after parsing the
   response, before applying anything**.
7. A new skill should not require Python changes unless it needs a genuinely
   new output format or context type. Most additions are pure config.

## The work package

State for a single pipeline run lives in **one git repo per run**, containing:

- `workpackage.yaml` — identity, lifecycle status, target info, inputs, stamps
  (one per completed stage), append-only history log, open issues, and an
  intervention block (populated only when escalated).
- `repo/` — the cloned Debian packaging repository as a subtree.

Stage progression uses gate policies (`resources/gates.yaml`). The expediter
reads the stamp for a completed stage and applies the gate policy to decide:
**promote**, **retry**, or **escalate**. Stamp results are `verified`,
`verified_with_warnings`, `rejected`, or `needs_human_review`.

When a manager escalates, the expediter populates `intervention:` in the YAML,
creates an `intervention/<id>` branch, and parks the run. The human clones the
work-package repo, pushes commits back, and the expediter resolves
clean-resume / auto-rebase / conflict.

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

## Reference

- Packastack: https://github.com/MylesJP/packastack — read the `skill-doctor`
  branch for the existing AI-assisted skill pattern that this project
  generalizes.
- OpenStack releases (upstream version source of truth):
  https://opendev.org/openstack/releases/src/branch/master/deliverables
- Ubuntu OpenStack Dev packaging repos (downstream packaging source of truth):
  https://code.launchpad.net/~ubuntu-openstack-dev/+git
