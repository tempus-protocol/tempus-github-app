# Dogfooding: every agent push to our own repositories goes through Guard

This is the runbook for the last item of Fase 2 in the Tempus plan: the project's own
repositories accept pushes from coding agents only through Tempus Guard for GitHub. It is
written for the `tempus-protocol` organization, but nothing in it is specific to us: it is
the same setup a customer performs, which is the point of doing it first on ourselves.

The rule it enforces is simple. **A coding agent never holds a GitHub credential.** It
commits locally, the Tempus Guard CLI signs an intent for those commits, the gate decides,
the App pushes, and the pull request shows the **Verified by Tempus** check. Humans keep
pushing with their own accounts as before.

## Scope

| Repository | Role | Agent pushes through Guard |
| --- | --- | --- |
| `tempus-protocol/tempus-ddb` | core | yes |
| `tempus-protocol/tempus-github-app` | this executor | yes |
| `tempus-protocol/tempus-guard-cli` | the local client | yes |
| `tempus-protocol/Tempus-Advance` | console (private) | yes |
| `Julio-Patron/jsonlogic-fast` | independent dependency | yes, same gate, its own tenant |
| `Julio-Patron/Decision-Database` | archived | no pushes at all |

Agent branches live under the `agent/` namespace; the policy below allows nothing else.

## 0. One-time: register and install the App (operator, in the GitHub UI)

1. Register the App **under the organization** from the manifest in this repository:
   GitHub → organization *Settings* → *Developer settings* → *GitHub Apps* → *New GitHub App*,
   with the permissions listed in [SETUP.md](SETUP.md#1-register-the-github-app) (`contents`,
   `issues`, `pull_requests`, `checks` read and write; webhooks can stay off for dogfooding).
   Keep it private for now; the Marketplace listing is a separate step
   ([MARKETPLACE.md](MARKETPLACE.md)).
2. Generate a private key and store it outside every checkout, for example
   `~/.tempus/guard/app.pem`, readable by the operator only. Note the Client ID.
3. Install the App on the five repositories above (*Only select repositories*). Read the
   installation id from the URL, or run `python scripts/get_installation_id.py`.

Nothing in these three steps can be done by an agent, and the `.pem` never enters a
repository or an agent's machine. `.gitignore` in every repository excludes `*.pem`,
`*.keys.json`, `*.db` and `.env`; check before the first commit anyway.

## 1. One-time: the gate (operator machine)

One gate, two tenants: `tempus-protocol` for the organization, `jsonlogic-fast` for the
dependency. Everything lives in `~/.tempus/guard/`.

```bash
mkdir -p ~/.tempus/guard && cd ~/.tempus/guard
python -m pip install "tempus-ddb>=0.7" "tempus-github-app>=0.2"
tempus init                                  # default-deny: no --allow-all-dev here
tempus keygen --output executor.keys.json    # the executor identity, kept next to the App key
EXECUTOR_PUB=$(python -c "import json; print(json.load(open('executor.keys.json'))['public_key'])")
```

Register one identity per agent runtime that will push (one key per machine and tool, so a
revocation is precise):

```bash
tempus keygen --output claude-code-laptop.keys.json
tempus register-agent --alias claude-code-laptop --agent-keyfile claude-code-laptop.keys.json \
  --metadata '{"tenant_id":"tempus-protocol"}'
tempus register-agent --alias guard-executor --agent-keyfile executor.keys.json \
  --metadata '{"tenant_id":"tempus-protocol"}'
```

The agent key file goes to the agent's machine (section 2) and nowhere else. Then the
policy: pushes only, only to our repositories, only by our executor, short permits.

```bash
cat > policy.tempus-protocol.json <<EOF
{
  "schema_version": "tempus.policy-bundle.v1",
  "policy_version": "tempus-protocol-guard-v1",
  "tenant_id": "tempus-protocol",
  "constraints": {
    "allowed_action_types": ["github.push_branch"],
    "allowed_resources": ["tempus-protocol/*"],
    "allowed_executors": ["$EXECUTOR_PUB"],
    "max_ttl_seconds": 300,
    "max_input_bytes": 262144,
    "allowed_currencies": []
  }
}
EOF
tempus install-policy --policy policy.tempus-protocol.json
tempus doctor                                # must report active_policy: PASS
```

Repeat with `tenant_id` `jsonlogic-fast` and `allowed_resources` `["Julio-Patron/jsonlogic-fast"]`
for the dependency. `github.create_pull_request` and `github.merge_pull_request` stay out of
the policy on purpose: opening and merging pull requests remain human actions during
dogfooding, so the check is something a person reads before approving.

Executor configuration, as a `.env` next to the gate (template: `.env.example`):

```bash
GITHUB_APP_CLIENT_ID=Iv1.xxxxxxxx
GITHUB_APP_PRIVATE_KEY_PATH=/home/op/.tempus/guard/app.pem
GITHUB_APP_INSTALLATION_ID=12345678
TEMPUS_GATE_ID=$(python -c "import json; print(json.load(open('keys.json'))['public_key'])")
TEMPUS_TENANT_ID=tempus-protocol
TEMPUS_EXECUTOR_DB=executor.db
TEMPUS_EXECUTOR_KEYFILE=executor.keys.json
TEMPUS_PUSH_BRANCH_PREFIX=agent/
TEMPUS_GATE_DB=tempus.db
TEMPUS_GATE_KEYFILE=keys.json
TEMPUS_BUNDLE_OUT=evidence/bundle.json
```

`GITHUB_APP_REPOSITORY` is passed per run (`--repository`), because one gate serves all five
repositories. Publish the gate's **public** key where reviewers can find it: the organization
README and the Pages site. That key, and nothing else, is what a reviewer needs.

## 2. One-time: each agent machine

```bash
python -m pip install tempus-guard-cli
export TEMPUS_TENANT_ID=tempus-protocol
export TEMPUS_AGENT_KEYFILE=~/.tempus/claude-code-laptop.keys.json   # from section 1
export TEMPUS_BASE=origin/main               # origin/master for Tempus-Advance and tempus-guard-cli
```

In every clone the agent works on:

```bash
tempus guard hook install                    # pre-push hook: refuses direct pushes
tempus guard hook status
```

The hook is the local reminder; the real control is that the agent's environment has no
GitHub token, SSH key or `gh` login. Check it once per machine:

```bash
gh auth status 2>&1 | grep -q "not logged" && echo "ok: no gh credential"
git config --get credential.helper                      # should print nothing for agent users
```

Humans working on the same machine keep their own OS user and credentials.

## 3. Every push (the agent's loop)

The agent commits on `agent/<topic>` as usual and, instead of `git push`:

```bash
tempus guard intent --branch agent/fix-123 --base origin/main \
  --tenant-id tempus-protocol --agent-keyfile "$TEMPUS_AGENT_KEYFILE" \
  --resource tempus-protocol/tempus-ddb > signed.json
```

`signed.json` (the intent, the agent id and the signature) and the git bundle the command
wrote are the only things that leave the agent's machine. On the operator side:

```bash
cd ~/.tempus/guard
tempus guard request --signed signed.json --gate-db tempus.db --gate-keyfile keys.json > permit.json
tempus-github-app-executor --permit permit.json --bundle agent.bundle \
  --repository tempus-protocol/tempus-ddb --create-checkpoint
```

The executor unbundles, re-derives `paths` and `diff_sha256`, pushes `agent/fix-123` with a
one-hour installation token, signs the outcome, closes the trace in the gate, exports the
evidence bundle and publishes **Verified by Tempus** on the pushed commit. A human then opens
the pull request from `agent/fix-123` and reviews it with the check in view.

During dogfooding the two sides run on the same machine with the gate's database local to
the operator user; the agent user has read access to nothing in `~/.tempus/guard/`. A
transport between the two (a queue, an HTTP endpoint) is Fase 4 work, not needed here.

## 4. Enforcement on GitHub

Agents cannot push directly because they hold no credential; branch rules make the rest
visible and keep humans honest:

- **Rulesets on `main`/`master`** (organization *Settings* → *Repository* → *Rulesets*):
  require a pull request before merging, and restrict who can push to the default branch to
  the operator's account and the App. Already true for `tempus-ddb` (protected `main`);
  apply the same ruleset to the other four.
- **Do not require the check on the default branch yet.** The check lands on the agent's
  pushed commit, not on human or Dependabot pull requests; requiring it would block those.
  Reviewers look for it on `agent/*` pull requests. Once every agent push goes through Guard
  for two weeks, add a ruleset for pull requests from `agent/**` that requires
  **Verified by Tempus**.
- **Revoke agent keys** that leave service:
  `tempus revoke-agent --public-key <agent public key> --reason "laptop retired"`; open permits
  die with the key.

## 5. What counts as done

Fase 2's dogfooding item is complete when, for each repository in the table, the most
recent agent-authored pull request shows a green **Verified by Tempus** check whose
*Details* link verifies in the public verifier, and `git log` of the default branch shows no
agent commit that arrived any other way. Keep the exported bundles under
`~/.tempus/guard/evidence/`; they are what we show to the first external installer, and the
first content for the Fase 2 "Marketplace" video.

The exit criterion of Fase 2 itself ("a repository that is not yours shows a pull request
with the verified check, and its owner installed Guard without talking to you") is not
dogfooding; it needs the Marketplace listing and is tracked in the plan.

## What this runbook does not cover

- Running the gate or the executor as a service, high availability, multi-operator gates.
- Agents opening or merging pull requests through Guard (`github.create_pull_request`,
  `github.merge_pull_request` are supported by the executor but left out of the dogfooding
  policy by choice).
- Anchoring the gate's checkpoints outside the operator. `tempus anchor` is on tempus-ddb's
  `main` for 0.8, unreleased, and this runbook does not use it yet. Without it a checkpoint's
  date is the gate's own claim. Even with it, a bundle does not prove that the gate's ledger is
  the only one.
