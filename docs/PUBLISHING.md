# Adeu Publishing & Registry Guidelines

Adeu's release pipeline is highly automated, but because we use a custom domain namespace (`ai.adeu/adeu`) for the official MCP registry, the final step requires local authorization.

Here is the full lifecycle of a release and how the registries interact.

## 1. The Release Pipeline

When you want to cut a new release:

1. **Bump Versions:** Run the bump script to synchronize versions across the Python backend, Node monorepo, manifest files, and the MCP registry payload (`server.json`).
   ```powershell
   python scripts/bump.py 1.7.0
   ```
2. **Commit & Tag:**
   ```powershell
   git add .
   git commit -m "chore: release 1.7.0"
   git tag v1.7.0
   git push origin main --tags
   ```

## 2. Automated CI/CD Execution

Pushing a `v*` tag triggers the `.github/workflows/release.yml` GitHub Action. This action automatically:
1. **Builds the codebase** for both Python and TypeScript.
2. **Publishes to PyPI** (`adeu`).
3. **Publishes to NPM** (`@adeu/core` and `@adeu/mcp-server`).
   * *Crucial Context:* The `package.json` for `@adeu/mcp-server` contains a hidden cryptographic field: `"mcpName": "ai.adeu/adeu"`. This proves to the MCP registry that the owner of the NPM package and the owner of the namespace are the same entity.
4. **Builds the MCPB Bundle** (`Adeu.mcpb`) for Claude Desktop.
5. **Publishes to Smithery:** It uses `scripts/patch_smithery_mcpb.py` to inject live schemas into the bundle and uses the hidden `SMITHERY_API_KEY` repository secret to publish directly to `https://smithery.ai/servers/adeu/adeu`.

## 3. Official MCP Registry (Manual Local Step)

Because we use the highly sought-after custom namespace `ai.adeu/adeu` (which currently holds the #7 spot on the registry), the MCP registry strictly enforces **DNS Authentication**.

To keep the Ed25519 private key completely secure, we do not put it in GitHub Actions. Instead, we publish the payload (`server.json`) locally *after* the GitHub Action successfully publishes to NPM.

### Prerequisites (Once per machine)
You need the official `mcp-publisher` CLI tool installed locally.
If it is missing, follow the [official installation guide](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/quickstart.mdx#step-3-install-mcp-publisher).

### Step 1: Authenticate Local Session
Authenticate your terminal session using the DNS private key. (Keep this key stored safely in your local password manager, NEVER in the repository).
```powershell
mcp-publisher login dns -domain adeu.ai -private-key <your-private-key-here>
```
*Note: This creates a local session token. You do not need to run this on every release, only when the session expires.*

### Step 2: Publish the Listing
**Do not run this until the GitHub Action has successfully published the new version to NPM.** The registry must scrape NPM for the `mcpName` field to validate the listing.

Once NPM is updated, run the publishing script from the repository root:
```powershell
.\scripts\publish_mcp_registry.ps1
```

If successful, you will see:
```text
Publishing to https://registry.modelcontextprotocol.io...
✓ Successfully published
✓ Server ai.adeu/adeu version 1.7.0
[SUCCESS] Successfully deployed!
```
