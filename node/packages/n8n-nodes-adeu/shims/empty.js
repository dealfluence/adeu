// FILE: node/packages/n8n-nodes-adeu/shims/empty.js
// Empty shim — Node already provides `process`, `setImmediate`, etc. globally.
// We use this to neutralize browser polyfills that get bundled transitively
// through `readable-stream`, `jszip`, etc.
module.exports = {};
