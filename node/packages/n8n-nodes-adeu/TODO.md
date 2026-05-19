Here is the complete, corrected official documentation. It accurately captures the n8n documentation's requirements for first-time npm configuration (Granular Tokens vs. Trusted Publishers), the provenance mandate, and exactly how to align the divergent version numbers (1.0.0 vs 1.7.1) safely before the first release.

You can save this as `docs/spec-n8n-publishing.md`.

***

# n8n Node Publishing & Release Pipeline Strategy

## 1. Objective
To integrate the `n8n-nodes-adeu` community node into the existing `adeu` monorepo CI/CD release pipeline. The node must be published to npm alongside `@adeu/core` and `@adeu/mcp-server` while strictly adhering to n8n Cloud's "Verified Publisher" requirements:
1. **Zero runtime dependencies**: Achieved natively via `tsup` bundling.
2. **Cryptographic Provenance**: Must be published via a GitHub Actions workflow with a Sigstore provenance statement (mandatory for all verified nodes as of May 2026).

## 2. Handling the Core Engine Update Edge Case (Synchronized Versioning)
**The Dilemma:** Because `n8n-nodes-adeu` bundles `@adeu/core` entirely at build time, any bug fixes to the redline engine will *not* automatically cascade to n8n users. The n8n node must be physically re-bundled and published under a new version number.

**The Solution:** We will lock `n8n-nodes-adeu` to the exact same release cadence and version string as the rest of the monorepo (currently `v1.7.x`). 
By adding the n8n node to `scripts/bump.py`, it will be bumped simultaneously with `@adeu/core`. During the GitHub Actions release, `npm run build --workspaces` will natively compile the fresh `@adeu/core`, bundle it into `n8n-nodes-adeu`, and publish both to npm. 
*Benefit:* If an n8n user installs `v1.8.0` of the node, we can guarantee with 100% certainty that it contains `v1.8.0` of the core engine.

---

## 3. Required Code Modifications (For the Next LLM)

The next LLM taking over the session should implement the following two steps to finalize the pipeline code:

### Step 1: Update the Version Bumping Script
Modify `scripts/bump.py` to include the n8n package in the synchronization array.
*   **Target File:** `scripts/bump.py`
*   **Action:** Add `"node/packages/n8n-nodes-adeu/package.json"` to the `FILES_TO_BUMP` list.

### Step 2: Implement Cryptographic Provenance in CI/CD
Modify the GitHub Actions workflow to attach npm provenance signatures to the published packages.
*   **Target File:** `.github/workflows/release.yml`
*   **Location:** Inside the `npm-publish` job, at the `Publish to NPM` step.
*   **Action:** Append `--provenance` to the `npm publish --workspaces --access public` command. *(Note: The job already correctly possesses the required `id-token: write` permission for Sigstore).*

---

## 4. First-Time Setup & Initial Release (For the Human Maintainer)

Before the GitHub Action can successfully publish the n8n node for the first time, you must align the versions and ensure npm permissions are correctly scoped.

### Step A: NPM Permissions Check (Crucial)
If your current GitHub Secret (`NPM_TOKEN`) is a **Granular Access Token** scoped *only* to `@adeu/core` and `@adeu/mcp-server`, the CI will fail when it tries to publish the new `n8n-nodes-adeu` package.
*   **Action Required:** Go to npmjs.com and either:
    1. Generate a new Granular Access Token that includes `n8n-nodes-adeu` (and update your GitHub Secret).
    2. *Or* configure **Trusted Publishers** (OIDC) for `n8n-nodes-adeu` linked to your GitHub repository and `release.yml` workflow, completely eliminating the need for the token.

### Step B: The "Catch-up" Version Bump
Your monorepo is currently at `1.7.1`, but the n8n node is at `1.0.0`. 
Once the LLM updates `bump.py`, run the script locally with your *current* monorepo version:
```bash
python scripts/bump.py 1.7.1
```
*Why this works:* The script explicitly checks old versions. It will gracefully skip `@adeu/core` and Python (since they are already 1.7.1) and will *only* bump `n8n-nodes-adeu` from 1.0.0 to 1.7.1, perfectly aligning the monorepo.

### Step C: Execute the Initial Release
1. Commit the aligned version: `git commit -am "chore: align n8n node version to monorepo and update CI"`
2. Tag and push: `git tag v1.7.1-n8n` (or whatever tag triggers your release, or trigger manually via GitHub UI if your workflow allows re-running on an existing tag).
3. Wait for the `npm-publish` GitHub Action to complete. It will successfully publish `n8n-nodes-adeu` with the required provenance signature.

### Step D: Submit for Verification
1. Navigate to [creators.n8n.io/nodes](https://creators.n8n.io/nodes) and log in via GitHub.
2. Click "Submit a node".
3. Enter the npm package name: `n8n-nodes-adeu`.
4. n8n's automated systems will ingest the package, verify the `dependencies` block is empty, and validate the `--provenance` signature logged in the public transparency ledger. Once approved, the node will be officially available inside the n8n UI globally.

---

## 5. Future Releases Workflow

Once the first-time setup is complete, future releases require zero extra overhead. 

When you make a change to the core engine, MCP server, or the n8n node:
1. Run `python scripts/bump.py 1.7.2` (Bumps all packages simultaneously).
2. Commit, push the `v1.7.2` tag.
3. The GitHub Action automatically builds the updated `@adeu/core`, bundles it seamlessly into the updated `n8n-nodes-adeu`, and publishes the synchronized `1.7.2` packages to npm with fresh provenance signatures.
4. n8n users will immediately see the update available in their n8n instance.