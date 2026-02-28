import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  buildYamlDocument,
  ensureTerminalNewline,
  parseYamlText,
} from "../src/yaml-document.js";

test("docs/related-works.yaml round-trips through the editor YAML helpers", async () => {
  const sourceText = await readFile(
    new URL("../../../docs/related-works.yaml", import.meta.url),
    "utf8",
  );

  const { data, yamlDocument } = parseYamlText(sourceText);
  const rebuiltDocument = buildYamlDocument(data, yamlDocument);
  const rebuiltText = ensureTerminalNewline(rebuiltDocument.toString());
  const reparsed = parseYamlText(rebuiltText);

  assert.deepStrictEqual(reparsed.data, data);
  assert.equal(rebuiltText.endsWith("\n"), true);
});
