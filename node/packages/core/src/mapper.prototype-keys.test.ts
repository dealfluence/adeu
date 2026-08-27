import { describe, it, expect } from "vitest";
import { createTestDocument, appendRawXml } from "./test-utils.js";
import { renumber_snapshot_ids } from "./mapper.js";
import { findAllDescendants } from "./docx/dom.js";

const NS =
  'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"';

// Two revisions sharing one old id: the remap must give them the same new id.
const twoInsWithId = (id: string) =>
  `<w:p ${NS}>` +
  `<w:ins w:id="${id}" w:author="A" w:date="2026-01-01T00:00:00Z"><w:r><w:t>one</w:t></w:r></w:ins>` +
  `<w:ins w:id="${id}" w:author="A" w:date="2026-01-01T00:00:00Z"><w:r><w:t>two</w:t></w:r></w:ins>` +
  `</w:p>`;

describe("renumber_snapshot_ids: w:id values that collide with Object.prototype keys", () => {
  it.each(["constructor", "__proto__"])(
    'renumbers a revision whose w:id is "%s"',
    async (badId) => {
      const doc = await createTestDocument();
      appendRawXml(doc, twoInsWithId(badId));

      const [chg_remap] = renumber_snapshot_ids(doc);

      const ids = findAllDescendants(doc.element, "w:ins").map((el) =>
        el.getAttribute("w:id"),
      );
      expect(ids).toEqual(["1", "1"]);
      expect(chg_remap[badId]).toBe("1");
    },
  );
});
