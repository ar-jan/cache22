import Jedison from "jedison";
import { dump as dumpYaml, load as loadYaml } from "js-yaml";
import {
  chooseYamlFileHandle,
  openYamlFile,
  saveYamlAs,
  saveYamlToHandle,
  supportsOpenFilePicker,
  supportsSaveFilePicker,
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
const REPO_FILE_LABEL = "docs/related-works.yaml (repo copy)";

const elements = {
  editorRoot: document.querySelector("#editor"),
  reloadRepoButton: document.querySelector("#reload-repo"),
  openLocalButton: document.querySelector("#open-local"),
  saveInPlaceButton: document.querySelector("#save-in-place"),
  saveAsButton: document.querySelector("#save-as"),
  downloadButton: document.querySelector("#download-yaml"),
  fileLabel: document.querySelector("#file-label"),
  fileHint: document.querySelector("#file-hint"),
  validationLabel: document.querySelector("#validation-label"),
  fileAccessLabel: document.querySelector("#file-access-label"),
  dirtyLabel: document.querySelector("#dirty-label"),
  message: document.querySelector("#message-toast"),
};

let schema = null;
let editor = null;
const state = {
  fileLabel: REPO_FILE_LABEL,
  fileHandle: null,
  dirty: false,
  preamble: "",
  busy: false,
};
const fileAccessCapabilities = {
  canOpen: supportsOpenFilePicker(),
  canSaveAs: supportsSaveFilePicker(),
};
const actionAvailability = {
  reloadRepo: true,
  openLocal: true,
  saveInPlace: true,
  saveAs: true,
  download: true,
};

let messageTimeout = null;

function setMessage(text, type = "info") {
  elements.message.textContent = text;
  elements.message.dataset.type = type;
  elements.message.classList.add("visible");

  if (messageTimeout) {
    clearTimeout(messageTimeout);
  }

  messageTimeout = setTimeout(() => {
    elements.message.classList.remove("visible");
  }, 5000);
}

function localHandleLabel(fileName) {
  return `${fileName} (local file)`;
}

function getHandleFileName(fileHandle) {
  return fileHandle?.name ?? DEFAULT_DOWNLOAD_NAME;
}

function updateFileUI() {
  elements.fileLabel.textContent = state.fileLabel;

  if (!state.fileHandle) {
    if (fileAccessCapabilities.canOpen) {
      elements.saveInPlaceButton.textContent = "Save In Place (choose file)";
      elements.fileHint.textContent = "Editing repository copy. Save In Place will prompt you to choose a local YAML file.";
      elements.fileHint.dataset.type = "warning";
      return;
    }

    elements.saveInPlaceButton.textContent = "Save In Place";
    if (fileAccessCapabilities.canSaveAs) {
      elements.fileHint.textContent = "Opening local files is unavailable. Use Save As first, then Save In Place will reuse that file.";
    } else {
      elements.fileHint.textContent = "Editing repository copy. File System Access is unavailable; use Download YAML and replace the file manually.";
    }
    elements.fileHint.dataset.type = "warning";
    return;
  }

  const fileName = getHandleFileName(state.fileHandle);
  elements.saveInPlaceButton.textContent = `Save In Place (${fileName})`;
  elements.fileHint.textContent = `Editing and Save In Place write to ${fileName}.`;
  elements.fileHint.dataset.type = "info";
}

function refreshActionAvailability() {
  actionAvailability.reloadRepo = true;
  actionAvailability.openLocal = fileAccessCapabilities.canOpen;
  actionAvailability.saveAs = fileAccessCapabilities.canSaveAs;
  actionAvailability.saveInPlace = Boolean(state.fileHandle) || fileAccessCapabilities.canOpen;
  actionAvailability.download = true;
}

function applyActionDisabledState() {
  elements.reloadRepoButton.disabled = state.busy || !actionAvailability.reloadRepo;
  elements.openLocalButton.disabled = state.busy || !actionAvailability.openLocal;
  elements.saveInPlaceButton.disabled = state.busy || !actionAvailability.saveInPlace;
  elements.saveAsButton.disabled = state.busy || !actionAvailability.saveAs;
  elements.downloadButton.disabled = state.busy || !actionAvailability.download;
}

function renderState() {
  elements.dirtyLabel.textContent = state.dirty ? "Yes" : "No";
  updateFileUI();
  refreshActionAvailability();
  applyActionDisabledState();
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

  return lines.slice(0, index).join("\n").replace(/\s+$/, "");
}

function parseYamlDocument(text) {
  return {
    data: parseYamlObject(text),
    preamble: extractYamlPreamble(text),
  };
}

function createEditor(data) {
  if (editor && typeof editor.destroy === "function") {
    editor.destroy();
  }

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

function applyLoadedDocument({ data, preamble, fileLabel, fileHandle = null }) {
  createEditor(data);
  transitionState({
    fileLabel,
    fileHandle,
    preamble,
    dirty: false,
  });
}

async function loadFromFileHandle(fileHandle, fileName = fileHandle?.name) {
  await ensureSchemaLoaded();
  const yamlText = await readTextFromFileHandle(fileHandle);
  const { data, preamble } = parseYamlDocument(yamlText);
  applyLoadedDocument({
    data,
    preamble,
    fileLabel: localHandleLabel(fileName ?? DEFAULT_DOWNLOAD_NAME),
    fileHandle,
  });
}

async function loadFromRepo() {
  await ensureSchemaLoaded();
  const yamlText = await fetchFirstAvailable(REPO_YAML_URL_CANDIDATES, (response) => response.text(), "text");
  const { data, preamble } = parseYamlDocument(yamlText);
  applyLoadedDocument({
    data,
    preamble,
    fileLabel: REPO_FILE_LABEL,
    fileHandle: null,
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

async function runAction(action) {
  if (state.busy) {
    return;
  }

  transitionState({ busy: true });
  try {
    await action();
  } catch (error) {
    setMessage(describeError(error), "error");
  } finally {
    transitionState({ busy: false });
  }
}

async function handleOpenLocalFile() {
  if (!confirmDiscardChanges("open a local YAML file")) {
    return;
  }

  await runAction(async () => {
    await ensureSchemaLoaded();
    const { fileHandle, fileName, text } = await openYamlFile();
    const { data, preamble } = parseYamlDocument(text);
    applyLoadedDocument({
      data,
      preamble,
      fileLabel: localHandleLabel(fileName),
      fileHandle,
    });
    await persistHandle(fileHandle);
    setMessage(`Loaded local YAML from ${fileName}.`, "success");
  });
}

async function ensureCurrentFileHandle() {
  if (state.fileHandle) {
    return state.fileHandle;
  }

  if (!fileAccessCapabilities.canOpen) {
    throw new Error("This browser cannot choose an existing file. Use Save As first.");
  }

  const { fileHandle, fileName } = await chooseYamlFileHandle();
  transitionState({
    fileHandle,
    fileLabel: localHandleLabel(fileName),
  });
  await persistHandle(fileHandle);
  return fileHandle;
}

async function handleSaveInPlace() {
  await runAction(async () => {
    const fileHandle = await ensureCurrentFileHandle();
    await saveYamlToHandle(fileHandle, getCurrentYamlText());
    transitionState({
      fileHandle,
      fileLabel: localHandleLabel(getHandleFileName(fileHandle)),
      dirty: false,
    });
    setMessage(`Saved ${getHandleFileName(fileHandle)}.`, "success");
  });
}

async function handleSaveAs() {
  await runAction(async () => {
    const fileHandle = await saveYamlAs(getSuggestedFileName(), getCurrentYamlText());
    await persistHandle(fileHandle);
    transitionState({
      fileHandle,
      fileLabel: localHandleLabel(getHandleFileName(fileHandle)),
      dirty: false,
    });
    setMessage(`Saved ${getHandleFileName(fileHandle)}.`, "success");
  });
}

async function handleDownload() {
  await runAction(async () => {
    const fileName = getSuggestedFileName();
    startDownload(fileName, getCurrentYamlText());
    setMessage(`Downloaded ${fileName}.`, "success");
  });
}

async function tryRestorePersistedHandle() {
  if (!supportsHandlePersistence()) {
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

  try {
    const permission = await fileHandle.queryPermission({ mode: "readwrite" });
    if (permission !== "granted") {
      return false;
    }
  } catch (error) {
    console.warn("Failed to query persisted handle permissions:", error);
    await clearPersistedHandle();
    return false;
  }

  try {
    await loadFromFileHandle(fileHandle, handleName);
    setMessage(`Loaded local YAML from persisted handle (${handleName}).`, "success");
    return true;
  } catch (error) {
    console.warn("Persisted handle is no longer usable; clearing it.", error);
    await clearPersistedHandle();
    return false;
  }
}

function bindEvents() {
  elements.reloadRepoButton.addEventListener("click", async () => {
    if (!confirmDiscardChanges("reload the repository YAML")) {
      return;
    }

    await runAction(async () => {
      await loadFromRepo();
    });
  });
  elements.openLocalButton.addEventListener("click", handleOpenLocalFile);
  elements.saveInPlaceButton.addEventListener("click", handleSaveInPlace);
  elements.saveAsButton.addEventListener("click", handleSaveAs);
  elements.downloadButton.addEventListener("click", handleDownload);
}

function setupFileAccessUI() {
  if (fileAccessCapabilities.canOpen && fileAccessCapabilities.canSaveAs) {
    elements.fileAccessLabel.textContent = "Open + Save As supported";
    return;
  }

  if (fileAccessCapabilities.canOpen) {
    elements.fileAccessLabel.textContent = "Open supported (Save As unavailable)";
    setMessage("Save As is unavailable in this browser. Use Save In Place or Download YAML.", "info");
    return;
  }

  if (fileAccessCapabilities.canSaveAs) {
    elements.fileAccessLabel.textContent = "Save As supported (Open unavailable)";
    setMessage("Opening local files is unavailable in this browser. Use Save As or Download YAML.", "info");
    return;
  }

  elements.fileAccessLabel.textContent = "Not supported in this browser";
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
