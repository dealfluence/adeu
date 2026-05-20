// FILE: node/packages/n8n-nodes-adeu/shims/module.js
module.exports = {
  createRequire: function () {
    return function () {
      return {}; // Returns an empty object so looking up .Worker doesn't crash
    };
  },
};
