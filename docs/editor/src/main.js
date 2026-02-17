import Jedison from "jedison";
import { dump as dumpYaml, load as loadYaml } from "js-yaml";
import {
  chooseYamlFileHandle,
  openYamlFile,
  saveYamlAs,
  saveYamlToHandle,
  supportsFileSystemAccess,
} from "./file-system-access.js";
import {
  clearStoredRelatedWorksHandle,
  readStoredRelatedWorksHandle,
  supportsHandlePersistence,
  writeStoredRelatedWorksHandle,
} from "./file-handle-store.js";

const REPO_SCHEMA_URL_CANDIDATES = [
  "/docs/related-works.schema.json",
  "../related-works.schema.json",
  "../../related-works.schema.json",
];
const REPO_YAML_URL_CANDIDATES = [
  "/docs/related-works.yaml",
  "../related-works.yaml",
  "../../related-works.yaml",
];
const DEFAULT_DOWNLOAD_NAME = "related-works.yaml";
const REPO_SOURCE_NAME = "docs/related-works.yaml (repo copy)";

const elements = {
  editorRoot: document.querySelector("#editor"),
  reloadRepoButton: document.querySelector("#reload-repo"),
  openLocalButton: document.querySelector("#open-local"),
  saveInPlaceButton: document.querySelector("#save-in-place"),
  saveAsButton: document.querySelector("#save-as"),
  changeSaveTargetButton: document.querySelector("#change-save-target"),
  downloadButton: document.querySelector("#download-yaml"),
  sourceLabel: document.querySelector("#source-label"),
  saveTargetLabel: document.querySelector("#save-target-label"),
  saveTargetHint: document.querySelector("#save-target-hint"),
  validationLabel: document.querySelector("#validation-label"),
  fileAccessLabel: document.querySelector("#file-access-label"),
  dirtyLabel: document.querySelector("#dirty-label"),
  message: document.querySelector("#message-toast"),
};

let schema = null;
let editor = null;
const state = {
  sourceType: "repo",
  sourceName: REPO_SOURCE_NAME,
  fileHandle: null,
  dirty: false,
  preamble: "",
};

let messageTimeout = null;

function setMessage(text, type = "info") {
  elements.message.textContent = text;
  elements.message.dataset.type = type;
  elements.message.classList.add("visible");

  if (messageTimeout) {
    clearTimeout(messageTimeout);
  }

  // Auto-hide after 5 seconds
  messageTimeout = setTimeout(() => {
    elements.message.classList.remove("visible");
  }, 5000);
}

function localHandleLabel(fileName) {
  return `${fileName} (local file handle)`;
}

function getHandleFileName(fileHandle) {
  return fileHandle?.name ?? DEFAULT_DOWNLOAD_NAME;
}

function getSaveTargetFileName() {
  return getHandleFileName(state.fileHandle);
}

function updateSaveTargetUI() {
  if (!state.fileHandle) {
    elements.saveTargetLabel.textContent = "Not selected (will prompt on first save)";
    elements.saveInPlaceButton.textContent = "Save In Place (choose file)";
    elements.saveTargetHint.textContent = "First Save In Place will prompt you to choose a YAML file.";
    elements.saveTargetHint.dataset.type = "info";
    return;
  }

  const targetName = getSaveTargetFileName();
  elements.saveTargetLabel.textContent = localHandleLabel(targetName);
  elements.saveInPlaceButton.textContent = `Save In Place (${targetName})`;

  if (state.sourceType === "repo") {
    elements.saveTargetHint.textContent = `Editing repo copy. Save In Place writes to ${targetName}.`;
    elements.saveTargetHint.dataset.type = "warning";
    return;
  }

  elements.saveTargetHint.textContent = `Save In Place writes to ${targetName}.`;
  elements.saveTargetHint.dataset.type = "info";
}

function renderState() {
  elements.sourceLabel.textContent = state.sourceName;
  elements.dirtyLabel.textContent = state.dirty ? "Yes" : "No";
  updateSaveTargetUI();
}

function transitionState(patch) {
  Object.assign(state, patch);
  renderState();
}

async function persistHandle(fileHandle) {
  if (!supportsHandlePersistence()) {
    return;
  }

  try {
    await writeStoredRelatedWorksHandle(fileHandle);
  } catch (error) {
    console.warn("Failed to persist file handle:", error);
  }
}

async function clearPersistedHandle() {
  if (!supportsHandlePersistence()) {
    return;
  }

  try {
    await clearStoredRelatedWorksHandle();
  } catch (error) {
    console.warn("Failed to clear persisted file handle:", error);
  }
}

function getEditorErrors() {
  if (!editor) {
    return [];
  }
  return editor.getErrors(["error"]);
}

function updateValidationLabel() {
  const errors = getEditorErrors();
  if (errors.length === 0) {
    elements.validationLabel.textContent = "Valid";
    return;
  }
  elements.validationLabel.textContent = `${errors.length} error(s)`;
}

function addBlankLinesBetweenProjectItems(yamlText) {
  const lines = yamlText.replace(/\n+$/, "").split("\n");
  const output = [];
  let insideProjects = false;

  for (const line of lines) {
    const isTopLevelKey = /^[^ \t][^:]*:\s*$/.test(line);
    if (isTopLevelKey) {
      insideProjects = line.trim() === "projects:";
    }

    if (insideProjects && line.startsWith("  - ")) {
      const previous = output[output.length - 1];
      if (previous && previous.trim() !== "" && previous.trim() !== "projects:") {
        output.push("");
      }
    }

    output.push(line);
  }

  return `${output.join("\n")}\n`;
}

function toYaml(value) {
  const dumped = dumpYaml(value, {
    noRefs: true,
    lineWidth: -1,
    sortKeys: false,
  });
  const body = addBlankLinesBetweenProjectItems(dumped.trimStart());
  if (state.preamble) {
    return `${state.preamble}\n${body}`.replace(/\n?$/, "\n");
  }
  return body.replace(/\n?$/, "\n");
}

function parseYamlObject(text) {
  const parsed = loadYaml(text);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("YAML top-level value must be an object.");
  }
  return parsed;
}

function extractYamlPreamble(text) {
  const lines = text.split(/\r?\n/);
  let index = 0;

  while (index < lines.length) {
    const trimmed = lines[index].trim();
    if (trimmed === "" || trimmed === "---" || trimmed.startsWith("#")) {
      index += 1;
      continue;
    }
    break;
  }

  const preamble = lines.slice(0, index).join("\n").replace(/\s+$/, "");
  return preamble;
}

function createEditor(data) {
  elements.editorRoot.replaceChildren();
  editor = new Jedison.Create({
    container: elements.editorRoot,
    theme: new Jedison.Theme(),
    schema,
    data,
  });
  editor.on("change", () => {
    transitionState({ dirty: true });
    updateValidationLabel();
  });
  updateValidationLabel();
}

async function fetchFirstAvailable(candidates, parser, kind) {
  const failures = [];

  for (const url of candidates) {
    try {
      const response = await fetch(url);
      if (!response.ok) {
        failures.push(`${url} -> ${response.status}`);
        continue;
      }
      return parser(response);
    } catch (error) {
      failures.push(`${url} -> ${describeError(error)}`);
    }
  }

  throw new Error(`Failed to load ${kind} from candidates: ${failures.join("; ")}`);
}

async function ensureSchemaLoaded() {
  if (!schema) {
    schema = await fetchFirstAvailable(REPO_SCHEMA_URL_CANDIDATES, (response) => response.json(), "JSON");
  }
}

async function readTextFromFileHandle(fileHandle) {
  const file = await fileHandle.getFile();
  return file.text();
}

function applyLoadedDocument({ data, preamble, sourceName, sourceType, fileHandle = state.fileHandle }) {
  createEditor(data);
  transitionState({
    sourceName,
    sourceType,
    fileHandle,
    preamble,
    dirty: false,
  });
}

async function loadFromFileHandle(fileHandle, fileName = fileHandle?.name) {
  await ensureSchemaLoaded();
  const yamlText = await readTextFromFileHandle(fileHandle);
  const data = parseYamlObject(yamlText);
  applyLoadedDocument({
    data,
    preamble: extractYamlPreamble(yamlText),
    sourceName: localHandleLabel(fileName ?? DEFAULT_DOWNLOAD_NAME),
    sourceType: "local",
    fileHandle,
  });
}

async function loadFromRepo() {
  await ensureSchemaLoaded();
  const yamlText = await fetchFirstAvailable(REPO_YAML_URL_CANDIDATES, (response) => response.text(), "text");
  const data = parseYamlObject(yamlText);
  applyLoadedDocument({
    data,
    preamble: extractYamlPreamble(yamlText),
    sourceName: REPO_SOURCE_NAME,
    sourceType: "repo",
  });
  setMessage("Loaded schema and repository YAML.", "success");
}

function getSuggestedFileName() {
  return getHandleFileName(state.fileHandle);
}

function startDownload(filename, contents) {
  const blob = new Blob([contents], { type: "application/yaml;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}

function getCurrentYamlText() {
  if (!editor) {
    throw new Error("Editor is not initialized.");
  }
  return toYaml(editor.getValue());
}

function describeError(error) {
  if (error?.name === "AbortError") {
    return "Action canceled.";
  }
  if (error instanceof Error && error.message) {
    return error.message;
  }
  return "Unexpected error.";
}

function confirmDiscardChanges(actionDescription) {
  if (!state.dirty) {
    return true;
  }

  return window.confirm(`You have unsaved changes. Discard them and ${actionDescription}?`);
}

async function handleOpenLocalFile() {
  if (!confirmDiscardChanges("open a local YAML file")) {
    return;
  }

  try {
    const { fileHandle, fileName, text } = await openYamlFile();
    const data = parseYamlObject(text);
    applyLoadedDocument({
      data,
      preamble: extractYamlPreamble(text),
      sourceName: localHandleLabel(fileName),
      sourceType: "local",
      fileHandle,
    });
    await persistHandle(fileHandle);
    setMessage(`Loaded local YAML from ${fileName}.`, "success");
  } catch (error) {
    setMessage(describeError(error), "error");
  }
}

async function handleChangeSaveTarget() {
  try {
    const { fileHandle, fileName } = await chooseYamlFileHandle();
    transitionState({ fileHandle });
    await persistHandle(fileHandle);
    setMessage(`Save target set to ${fileName}.`, "success");
  } catch (error) {
    setMessage(describeError(error), "error");
  }
}

async function ensureCurrentFileHandle() {
  if (state.fileHandle) {
    return state.fileHandle;
  }

  const { fileHandle } = await chooseYamlFileHandle();
  transitionState({ fileHandle });
  await persistHandle(fileHandle);
  return state.fileHandle;
}

async function handleSaveInPlace() {
  try {
    const fileHandle = await ensureCurrentFileHandle();
    await saveYamlToHandle(fileHandle, getCurrentYamlText());
    transitionState({ fileHandle, dirty: false });
    setMessage(`Saved ${getSaveTargetFileName()}.`, "success");
  } catch (error) {
    setMessage(describeError(error), "error");
  }
}

async function handleSaveAs() {
  try {
    const fileHandle = await saveYamlAs(getSuggestedFileName(), getCurrentYamlText());
    await persistHandle(fileHandle);

    const patch = {
      fileHandle,
      dirty: false,
    };

    if (state.sourceType === "local") {
      patch.sourceName = localHandleLabel(getHandleFileName(fileHandle));
    }

    transitionState(patch);
    setMessage(`Saved ${getHandleFileName(fileHandle)}.`, "success");
  } catch (error) {
    setMessage(describeError(error), "error");
  }
}

function handleDownload() {
  try {
    const fileName = getSuggestedFileName();
    startDownload(fileName, getCurrentYamlText());
    setMessage(`Downloaded ${fileName}.`, "success");
  } catch (error) {
    setMessage(describeError(error), "error");
  }
}

async function tryRestorePersistedHandle() {
  if (!supportsFileSystemAccess() || !supportsHandlePersistence()) {
    return false;
  }

  let fileHandle = null;
  try {
    fileHandle = await readStoredRelatedWorksHandle();
  } catch (error) {
    console.warn("Failed to read persisted file handle:", error);
    return false;
  }

  if (!fileHandle) {
    return false;
  }

  if (typeof fileHandle.getFile !== "function") {
    await clearPersistedHandle();
    return false;
  }

  const handleName = getHandleFileName(fileHandle);
  transitionState({ fileHandle });

  try {
    const permission = await fileHandle.queryPermission({ mode: "readwrite" });
    if (permission !== "granted") {
      return false;
    }
  } catch (error) {
    console.warn("Failed to query persisted handle permissions:", error);
    transitionState({ fileHandle: null });
    await clearPersistedHandle();
    return false;
  }

  try {
    await loadFromFileHandle(fileHandle, handleName);
    setMessage(`Loaded local YAML from persisted handle (${handleName}).`, "success");
    return true;
  } catch (error) {
    console.warn("Persisted handle is no longer usable; clearing it.", error);
    transitionState({ fileHandle: null });
    await clearPersistedHandle();
    return false;
  }
}

function bindEvents() {
  elements.reloadRepoButton.addEventListener("click", async () => {
    if (!confirmDiscardChanges("reload the repository YAML")) {
      return;
    }

    try {
      await loadFromRepo();
    } catch (error) {
      setMessage(describeError(error), "error");
    }
  });
  elements.openLocalButton.addEventListener("click", handleOpenLocalFile);
  elements.saveInPlaceButton.addEventListener("click", handleSaveInPlace);
  elements.saveAsButton.addEventListener("click", handleSaveAs);
  elements.changeSaveTargetButton.addEventListener("click", handleChangeSaveTarget);
  elements.downloadButton.addEventListener("click", handleDownload);
}

function setupFileAccessUI() {
  if (supportsFileSystemAccess()) {
    elements.fileAccessLabel.textContent = "Supported";
    return;
  }

  elements.fileAccessLabel.textContent = "Not supported in this browser";
  elements.openLocalButton.disabled = true;
  elements.saveInPlaceButton.disabled = true;
  elements.saveAsButton.disabled = true;
  elements.changeSaveTargetButton.disabled = true;
  setMessage(
    "File System Access API is unavailable. Use Download YAML and replace the file manually.",
    "info",
  );
}

async function initialize() {
  bindEvents();
  setupFileAccessUI();
  renderState();

  const loadedFromPersistedHandle = await tryRestorePersistedHandle();
  if (!loadedFromPersistedHandle) {
    await loadFromRepo();
  }
}

initialize().catch((error) => {
  setMessage(describeError(error), "error");
});
