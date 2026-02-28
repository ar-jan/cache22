import { isMap, isPair, isSeq, parseDocument } from "yaml";

export function normalizeLineEndings(text) {
  return text.replace(/\r\n?/g, "\n");
}

export function ensureTerminalNewline(text) {
  return text.replace(/\n?$/, "\n");
}

function isPlainObject(value) {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

export function parseYamlObject(value) {
  if (!isPlainObject(value)) {
    throw new Error("YAML top-level value must be an object.");
  }
  return value;
}

function createEmptyYamlDocument() {
  const yamlDocument = parseDocument("", {
    prettyErrors: true,
  });
  if (yamlDocument.errors.length > 0) {
    throw yamlDocument.errors[0];
  }
  return yamlDocument;
}

function applySequenceSpacing(node) {
  if (!node) {
    return;
  }

  if (isSeq(node)) {
    node.items.forEach((item, index) => {
      if (item && typeof item === "object") {
        item.spaceBefore = index > 0;
      }
      applySequenceSpacing(item);
    });
    return;
  }

  if (isMap(node)) {
    node.items.forEach((pair) => applySequenceSpacing(pair));
    return;
  }

  if (isPair(node)) {
    applySequenceSpacing(node.key);
    applySequenceSpacing(node.value);
  }
}

function preserveRootPresentation(sourceNode, targetNode) {
  if (!sourceNode || !targetNode) {
    return;
  }

  targetNode.commentBefore = sourceNode.commentBefore ?? null;
  targetNode.comment = sourceNode.comment ?? null;
  targetNode.spaceBefore = sourceNode.spaceBefore ?? false;
}

export function buildYamlDocument(data, previousDocument = null) {
  const value = parseYamlObject(data);
  const yamlDocument = previousDocument
    ? previousDocument.clone()
    : createEmptyYamlDocument();
  const previousContents = yamlDocument.contents;

  yamlDocument.contents = yamlDocument.createNode(value);
  preserveRootPresentation(previousContents, yamlDocument.contents);
  applySequenceSpacing(yamlDocument.contents);
  return yamlDocument;
}

export function parseYamlText(text) {
  const yamlDocument = parseDocument(normalizeLineEndings(text), {
    prettyErrors: true,
  });

  if (yamlDocument.errors.length > 0) {
    throw yamlDocument.errors[0];
  }

  const data = parseYamlObject(yamlDocument.toJS());
  return { data, yamlDocument };
}
