# test_resources

Fixtures for scenario tests under `tests/`. Parallel to `resources/`.

## Layout

```
test_resources/
├── workpackages/                # starting-state workpackage.yaml fixtures
├── skill_responses/             # canned LLM output per skill, per scenario
│   └── detector/
└── builds/                      # real packastack build outputs (one dir per package+version)
```

## What these are for

The pipeline calls LLMs through skills. Tests need to exercise manager
behaviour without invoking an LLM. So for each skill we keep canned response
files that look identical to what the LLM would emit. A test loads a starting
workpackage into a temp git repo, feeds a canned response through the runner,
and asserts the resulting workpackage state.

`builds/` holds real packastack build outputs captured at known-good points,
one subdirectory per package+version. Currently:

- `ironic-ui_6.8.0-0ubuntu1/` — full Debian build output from a successful
  packastack run on 2026-04-14: source `.dsc` + tarballs, binary `.deb`,
  build/buildinfo/changes records, upstream signature. See its
  `PROVENANCE.md` for the originating cache path.

These are the reference inputs for the **QA validator skill (WIP)** and any
downstream stage that consumes a build artifact. The QA skill, its manager,
and the runner are all still being scaffolded; tests in `tests/` currently
exercise the detection stage only.
