# @adeu/core

[![GitHub Repo stars](https://img.shields.io/github/stars/dealfluence/adeu?style=social)](https://github.com/dealfluence/adeu)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](https://opensource.org/licenses/MIT)

**The AI-native Virtual DOM for Microsoft Word (TypeScript Engine)**

`@adeu/core` is a zero-dependency TypeScript library that allows AI agents and LLMs to safely read and edit Microsoft Word (`.docx`) files. It translates complex OpenXML into token-efficient CriticMarkup (Markdown) and applies AI text edits as native Word Tracked Changes and Comments.

This is the pure TypeScript implementation of the [Adeu Python SDK](https://github.com/dealfluence/adeu), built on a purpose-built OOXML parser and `fflate` to run entirely in Node.js.

## Installation

```bash
npm install @adeu/core
```

## Quick Start

```typescript
import { readFileSync, writeFileSync } from "fs";
import { 
  DocumentObject, 
  RedlineEngine, 
  extractTextFromBuffer 
} from "@adeu/core";

async function main() {
  const buffer = readFileSync("contract.docx");

  // 1. Extract to CriticMarkup for an LLM to read
  const markdown = await extractTextFromBuffer(buffer, false);
  console.log(markdown); 

  // 2. Load the document DOM
  const doc = await DocumentObject.load(buffer);
  const engine = new RedlineEngine(doc, "AI Reviewer");

  // 3. Apply an edit as a native Tracked Change
  engine.process_batch([
    {
      type: "modify",
      target_text: "State of New York",
      new_text: "State of Delaware",
      match_mode: "all",
      comment: "Standardizing governing law."
    }
  ]);

  // 4. Save the manipulated DOCX
  const outBuffer = await doc.save();
  writeFileSync("contract_redlined.docx", outBuffer);
}

main();
```

## Documentation & Support
For full architectural details, API usage, and the project constitution, please visit the [main Adeu repository](https://github.com/dealfluence/adeu) or our [website](https://adeu.ai).