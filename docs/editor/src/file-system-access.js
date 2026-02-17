const YAML_FILE_PICKER_TYPES = [
  {
    description: "YAML files",
    accept: {
      "application/yaml": [".yaml", ".yml"],
      "text/yaml": [".yaml", ".yml"],
      "text/plain": [".yaml", ".yml"],
    },
  },
];

export function supportsOpenFilePicker() {
  return typeof window.showOpenFilePicker === "function";
}

export function supportsSaveFilePicker() {
  return typeof window.showSaveFilePicker === "function";
}

export function supportsFileSystemAccess() {
  return supportsOpenFilePicker() && supportsSaveFilePicker();
}

async function ensureReadWritePermission(fileHandle) {
  const options = { mode: "readwrite" };

  if ((await fileHandle.queryPermission(options)) === "granted") {
    return true;
  }

  if ((await fileHandle.requestPermission(options)) === "granted") {
    return true;
  }

  return false;
}

async function pickYamlFileHandle() {
  if (!supportsOpenFilePicker()) {
    throw new Error("This browser does not support opening local files via the File System Access API.");
  }

  const [fileHandle] = await window.showOpenFilePicker({
    multiple: false,
    types: YAML_FILE_PICKER_TYPES,
    excludeAcceptAllOption: false,
  });

  return fileHandle;
}

export async function chooseYamlFileHandle() {
  const fileHandle = await pickYamlFileHandle();
  return {
    fileHandle,
    fileName: fileHandle.name,
  };
}

export async function openYamlFile() {
  const fileHandle = await pickYamlFileHandle();
  const file = await fileHandle.getFile();
  const text = await file.text();

  return {
    fileHandle,
    fileName: file.name,
    text,
  };
}

export async function saveYamlToHandle(fileHandle, yamlText) {
  if (!fileHandle) {
    throw new Error("No file handle was provided.");
  }

  const permissionGranted = await ensureReadWritePermission(fileHandle);
  if (!permissionGranted) {
    throw new Error("Write permission was not granted.");
  }

  const writable = await fileHandle.createWritable();
  try {
    await writable.write(yamlText);
    await writable.close();
  } catch (error) {
    try {
      await writable.abort();
    } catch {
      // Ignore abort errors and surface the original write failure.
    }
    throw error;
  }
}

export async function saveYamlAs(suggestedName, yamlText) {
  if (!supportsSaveFilePicker()) {
    throw new Error("This browser does not support saving local files via the File System Access API.");
  }

  const fileHandle = await window.showSaveFilePicker({
    suggestedName,
    types: YAML_FILE_PICKER_TYPES,
    excludeAcceptAllOption: false,
  });

  await saveYamlToHandle(fileHandle, yamlText);
  return fileHandle;
}
