# detector

First-stage skill. Decides whether a tracked OpenStack package has a new
upstream release that is not yet packaged in Ubuntu and not already in flight
via an open Launchpad merge proposal.

Inputs (env vars consumed by the pre-context scripts):

- `PACKAGE` — e.g. `nova`
- `OPENSTACK_SERIES` — e.g. `2024.1` or `caracal`
- `UBUNTU_SERIES` — e.g. `noble` (used only in the LLM output, not for fetching)

Pre-context scripts (run by the skill runner before the LLM call):

- `fetch_upstream.py` — latest release from
  `opendev.org/openstack/releases/deliverables/<series>/<package>.yaml`
- `fetch_packaged.py` — top `debian/changelog` version on the `master` branch
  of `~ubuntu-openstack-dev/+git/<package>`
- `fetch_mrs.py` — open merge proposals against that repo, plus their source
  branch's top changelog version when reachable

Output: a `routing` block declaring `NEEDS_RELEASE`, `UP_TO_DATE`,
`IN_FLIGHT`, or `NEEDS_HUMAN_REVIEW`. See `SKILL.md` for the exact format.
