# Setup and Registration Guide: Tempus Guard for GitHub

This guide explains how to register, install, and operate Tempus Guard for GitHub: credential-isolated, mediated execution with a "Verified by Tempus" check on every change.

---

## 1. Register the GitHub App

You can register the App manually or via the App Manifest (`manifest/app.yml`).

1. Go to **GitHub Settings → Developer settings → GitHub Apps → New GitHub App**.
2. **App Name**: Choose a unique name (e.g., `tempus-guard-acme`).
3. **Homepage URL**: `https://github.com/tempus-protocol/tempus-ddb` (or your organization's URL).
4. **Permissions**:
   - **Repository permissions**:
     - **Issues**: `Read and write`
     - **Pull requests**: `Read and write`
     - **Contents**: `Read and write` (required for pushing branches and merging pull requests)
     - **Checks**: `Read and write` (required for the "Verified by Tempus" check run)
     - **Metadata**: `Read-only` (implicit)
5. **Webhooks** (Optional):
   - If using the webhook service, provide your server URL and generate a secret string.
   - If using executor-only CLI mode, webhooks can be disabled.
6. Click **Create GitHub App**.

---

## 2. Generate RSA Private Key & Record IDs

1. In the newly created App's settings, scroll down to **Private keys** and click **Generate a private key**.
2. Download the `.pem` file and save it securely (e.g., `secrets/tempus-app.pem`).
3. Note the **Client ID** (e.g., `Iv1.xxxxxxxxxxxx`) and **App ID**.

> [!CAUTION]
> Never commit the `.pem` private key to source control. Ensure file permissions are restricted (`chmod 600` on Linux/macOS).

---

## 3. Install the App on Repositories

1. In the App settings sidebar, click **Install App**.
2. Select your user account or organization, and choose **Only select repositories**.
3. Select the target repositories (e.g., `owner/repository`).
4. Complete the installation and note the **Installation ID** from the URL (e.g., `https://github.com/settings/installations/12345678`).

---

## 4. Execution Boundary with Tempus DDB

The GitHub App RSA key remains strictly inside the mediated executor process. The requesting agent **never** receives GitHub tokens or the App private key.

1. **Agent signs Intent**: Agent declares action (e.g. `github.create_issue`) signed with Ed25519.
2. **Gate authorizes Permit**: Tempus Gate checks tenant policy and issues an expiring, single-use signed Permit (`ALLOWED`).
3. **Mediated Executor executes**:
   ```bash
   tempus-github-app-executor \
     --permit permit.json \
     --executor-db executor.db \
     --executor-keyfile executor.keys.json \
     --gate-id GATE_PUBLIC_KEY \
     --tenant-id TENANT_ID \
     --app-client-id Iv1.xxxxxxxx \
     --app-private-key secrets/tempus-app.pem \
     --installation-id 12345678 \
     --repository owner/repository
   ```
   Or using `.env`:
   ```bash
   tempus-github-app-executor --permit permit.json
   ```
4. **Receipt produced**: Both executor and Gate sign the outcome, producing a mathematical audit receipt.
5. **Offline trace verification**:
   ```bash
   tempus verify-trace --action-id <ACTION_ID>
   ```

---

## 5. Webhook Server Setup (Optional)

To receive webhooks (e.g., issue opened, PR created):

```bash
tempus-github-app-server --secret GITHUB_WEBHOOK_SECRET --port 8000
```

Configure your GitHub App's Webhook URL to point to `https://your-domain.com/webhook` and ensure HMAC verification is enabled.

## 6. Enable governed pull request merges

For existing Apps, update **Repository permissions → Contents** to **Read and write**.
Existing installations must accept the new permissions before merges can authenticate.
Updating this repository's manifest does not update an already registered App or installation.
Merge tokens are scoped to `contents: write` and the operator-bound repository.

The signed intent for `github.merge_pull_request` must include a positive integer
`pull_number`, the audited HEAD `sha` (exactly 40 hexadecimal characters), and an
explicit `merge_method` (`merge`, `squash`, or `rebase`). Optional `commit_title` and
`commit_message` must be strings. No other fields are accepted.

Before issuing merge permits, configure the tenant's Gate policy to require an
integration/release-manager role and human approval or dual signatures for protected
branches such as `main`. These are Gate policy responsibilities; enabling this action
does not install that policy or implement human approval in this executor. The executor
verifies the signed permit and executes its bound repository, PR, SHA, and method.
Keep GitHub branch protections enabled and require a new audited permit if HEAD changes.

Only a response containing boolean `merged: true` yields success. HTTP 405 means the
PR cannot be merged; HTTP 409 means HEAD differs from the authorized SHA. Both fail
without retry. Ambiguous results remain `UNKNOWN` and require read-only reconciliation.
See [GitHub's merge endpoint](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request).

## 7. Publish the "Verified by Tempus" check (Guard flow)

For existing Apps, add **Repository permissions → Checks → Read and write** and let each
installation accept the new permission. The check run token is scoped to `checks: write` and the
operator-bound repository, and is minted only by the check publisher, never by an action.

Run the executor with the gate at hand, so it can close the trace and export the evidence:

```bash
tempus-github-app-executor \
  --permit permit.json --bundle agent.bundle \
  --gate-db gate.db --gate-keyfile gate.keys.json \
  --bundle-out evidence/${ACTION_ID}.json
```

or set `TEMPUS_GATE_DB` and `TEMPUS_GATE_KEYFILE` in `.env`. The gate key never leaves this
process either: keep the gate database and key file readable by the executor only. What the
flow does, step by step, and what each exit code means, is in the
[README](../README.md#the-verified-by-tempus-check-guard-flow).

What a reviewer sees on the pull request: a check named **Verified by Tempus** (green only when
the evidence verifies and the action succeeded), whose *Details* link opens the public verifier
with the bundle digest and the gate key pre-filled, and whose text embeds the evidence bundle.
Pasting that bundle into <https://tempus-protocol.github.io/tempus-ddb/verify.html> re-verifies
every signature offline; the page says what a bundle cannot show (single-use consumption lives
in the gate's ledger).

On the agent's machine, [the Tempus Guard CLI](https://github.com/tempus-protocol/Universal-Kernel-Ciel) produces
the signed `github.push_branch` intent with the bundle, the paths and `diff_sha256`
(`tempus guard intent --branch agent/fix-123 --base origin/main ...`),
and `tempus guard hook install` refuses direct pushes from that clone.

## 8. Dogfooding

How the tempus-protocol repositories themselves accept agent pushes only through Guard, end to end
on one machine: [DOGFOODING.md](DOGFOODING.md).
