const DB_NAME = "cache22-editor";
const DB_VERSION = 1;
const STORE_NAME = "settings";
const RELATED_WORKS_HANDLE_KEY = "related-works-handle";

function openDb() {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);

    request.onerror = () => {
      reject(request.error ?? new Error("Failed to open IndexedDB."));
    };

    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(STORE_NAME)) {
        db.createObjectStore(STORE_NAME);
      }
    };

    request.onsuccess = () => {
      resolve(request.result);
    };
  });
}

function runRequest(request, fallbackMessage) {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => {
      resolve(request.result);
    };
    request.onerror = () => {
      reject(request.error ?? new Error(fallbackMessage));
    };
  });
}

function waitForTransaction(tx, fallbackMessage) {
  return new Promise((resolve, reject) => {
    tx.oncomplete = () => {
      resolve();
    };
    tx.onerror = () => {
      reject(tx.error ?? new Error(fallbackMessage));
    };
    tx.onabort = () => {
      reject(tx.error ?? new Error(fallbackMessage));
    };
  });
}

async function withStore(mode, operation, fallbackMessage) {
  const db = await openDb();
  try {
    const tx = db.transaction(STORE_NAME, mode);
    const store = tx.objectStore(STORE_NAME);
    const transactionDone = waitForTransaction(tx, fallbackMessage);
    const result = await operation(store);
    await transactionDone;
    return result;
  } finally {
    db.close();
  }
}

export function supportsHandlePersistence() {
  return typeof indexedDB !== "undefined";
}

export async function readStoredRelatedWorksHandle() {
  const result = await withStore(
    "readonly",
    (store) => runRequest(store.get(RELATED_WORKS_HANDLE_KEY), "Failed to read stored file handle."),
    "Failed to read stored file handle.",
  );
  return result ?? null;
}

export async function writeStoredRelatedWorksHandle(fileHandle) {
  await withStore(
    "readwrite",
    (store) => runRequest(store.put(fileHandle, RELATED_WORKS_HANDLE_KEY), "Failed to persist file handle."),
    "Failed to persist file handle.",
  );
}

export async function clearStoredRelatedWorksHandle() {
  await withStore(
    "readwrite",
    (store) => runRequest(store.delete(RELATED_WORKS_HANDLE_KEY), "Failed to clear stored file handle."),
    "Failed to clear stored file handle.",
  );
}
