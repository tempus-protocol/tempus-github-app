# GitHub Marketplace listing — draft

Working draft for the Tempus Guard for GitHub listing, written to be pasted into the
Marketplace form. Pricing and the publishing organization follow PLAN.md (decided
2026-10-31); the support contact is still a placeholder. The listing is published by the
`tempus-protocol` organization once the live repositories are transferred to it.

## Listing name

Tempus Guard for GitHub

## Tagline (one line)

Every agent change arrives with evidence.

## Category

Code review · Security · Compliance (pick up to two: **Security**, **Code review**)

## Short description

Coding agents never hold a GitHub credential. A Tempus gate issues a signed, single-use
permit for each push or merge; Tempus Guard performs the write and attaches a "Verified by
Tempus" check whose evidence anyone can verify in a browser with the gate's public key.

## Full description

**The problem.** Teams are letting coding agents open pull requests, push branches and
merge. Today that means handing the agent a token with write access and trusting logs that
the agent could also write.

**What Tempus Guard does.** The agent commits locally and asks for a permit: a
`tempus.action-intent.v1` signed with its own key, naming the exact tip, the base, the git
bundle digest, every changed path and the digest of the change. The gate evaluates your
policy and signs a single-use permit. Tempus Guard consumes the permit, mints a short-lived,
repository-scoped installation token, re-derives every bound value from the bundle and
performs exactly one write. Then it closes the trace in the gate, exports the evidence
bundle, verifies it against the pinned gate key and publishes a **Verified by Tempus** check
on the commit it produced.

**What a reviewer gets.** A check that is green only when the evidence verifies and the
action succeeded; a details link that opens the public verifier with the bundle and the
gate key pre-filled; the bundle itself in the check text. Verification runs offline, in
the browser, with no Tempus install: the same WebAssembly build of the protocol code the
CLI uses.

**What never happens.** The agent never sees a GitHub token or the App's private key. A
permit is consumed once; a replay fails before GitHub is contacted. Branches outside the
agent namespace (default `agent/`) are refused before any token is minted. Pushes are
lease-guarded fast-forwards from the signed base, or creations, and nothing else.

**Where policy lives.** In the gate, with one evaluator (`jsonlogic-fast`), signed per
tenant. Tempus Guard enforces what the permit says; it adds no policy language of its own.

**On the agent's side.** the Tempus Guard CLI (`tempus guard intent`, also `tempus guard`) builds and signs the
push intent from a local branch and installs a pre-push hook that refuses direct pushes.

## Supported actions

| Action | What the permit binds |
| --- | --- |
| Push a branch | tip, base, bundle digest, changed paths, diff digest, namespace |
| Create a pull request | title, head, base, body, draft |
| Merge a pull request | PR number, head SHA, merge method |
| Create issue, comment, labels, review requests | strict, allowlisted inputs |

## Permissions requested and why

| Permission | Why |
| --- | --- |
| Contents: read and write | Push agent branches, merge pull requests |
| Pull requests: read and write | Open pull requests, request reviews |
| Issues: read and write | Issues, comments, labels |
| Checks: read and write | Publish the "Verified by Tempus" check run |
| Metadata: read | Required by GitHub for every App |

## Screenshots to prepare

1. A pull request with the "Verified by Tempus" check green, details link visible.
2. The public verifier page showing a VERIFIED bundle with the trust section expanded.
3. The same check failing (`INVALID`), with the verifier error codes in the text.
4. A terminal: `tempus guard intent ...` output and the executor's Guard output side by side.

## Pricing (PLAN.md, 2026-10-31)

| Plan | Price | Includes |
| --- | --- | --- |
| Public repositories | Free, unlimited | Every verified action on public repositories |
| Private repositories, free tier | Free | 200 verified actions per month per organization |
| Private, pay per action | 0.05 USD per verified action beyond the free tier | Evidence retention for 1 year |
| Private, organization plan | 49 USD per month per organization | Up to 2,000 verified actions per month |

A "verified action" is one permit executed by Tempus Guard with its "Verified by Tempus"
check published. The protocol, the core and this App are MIT; what is sold is the hosted
gate, evidence retention and support. These numbers are reviewed with the first five
external repositories of the Fase 2 pilot.

## Support and links

- Documentation: README and `docs/SETUP.md` of this repository
- Verifier: <https://tempus-protocol.github.io/tempus-ddb/verify.html>
- Protocol: <https://github.com/tempus-protocol/tempus-ddb>
- Publisher: the `tempus-protocol` GitHub organization (<https://github.com/tempus-protocol>)
- Support contact: `__` (maintainer decision)
- Privacy statement: the App stores no repository content; evidence bundles contain the
  permit, the outcome and signatures, never source code. Tokens live for one hour at most
  and only inside the executor process.

## Pre-publication checklist (Marketplace requirements)

- [ ] App is public (`public: true` in the manifest) and installed on at least one
      repository of the publishing organization
- [ ] Webhook endpoint answers GitHub's ping (if webhooks stay enabled)
- [ ] Listing has a logo (use the Tempus mark; one brand), tagline, description, screenshots
- [ ] Paid plans entered as above and the Marketplace billing webhook handled
- [ ] Support contact filled in
- [ ] Repositories transferred to `tempus-protocol` and verified publisher status obtained
