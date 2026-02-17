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

function runTransaction(mode, operation) {
  return openDb().then(
    (db) =>
      new Promise((resolve, reject) => {
        const tx = db.transaction(STORE_NAME, mode);
        const store = tx.objectStore(STORE_NAME);
        let result;
        let operationError = null;
        let settled = false;

        const settle = (fn, value) => {
          if (settled) {
            return;
          }
          settled = true;
          db.close();
          fn(value);
        };

        const fail = (error) => {
          operationError = error ?? new Error("IndexedDB operation failed.");
          try {
            tx.abort();
          } catch {
            settle(reject, operationError);
          }
        };

        tx.oncomplete = () => {
          settle(resolve, result);
        };

        tx.onerror = () => {
          settle(reject, operationError ?? tx.error ?? new Error("IndexedDB transaction failed."));
        };

        tx.onabort = () => {
          settle(reject, operationError ?? tx.error ?? new Error("IndexedDB transaction aborted."));
        };

        try {
          operation(
            store,
            (value) => {
              result = value;
            },
            fail,
          );
        } catch (error) {
          fail(error);
        }
      }),
  );
}

export function supportsHandlePersistence() {
  return typeof indexedDB !== "undefined";
}

export async function readStoredRelatedWorksHandle() {
  return runTransaction("readonly", (store, setResult, fail) => {
    const request = store.get(RELATED_WORKS_HANDLE_KEY);
    request.onsuccess = () => {
      setResult(request.result ?? null);
    };
    request.onerror = () => {
      fail(request.error ?? new Error("Failed to read stored file handle."));
    };
  });
}

export async function writeStoredRelatedWorksHandle(fileHandle) {
  return runTransaction("readwrite", (store, setResult, fail) => {
    const request = store.put(fileHandle, RELATED_WORKS_HANDLE_KEY);
    request.onsuccess = () => {
      setResult(undefined);
    };
    request.onerror = () => {
      fail(request.error ?? new Error("Failed to persist file handle."));
    };
  });
}

export async function clearStoredRelatedWorksHandle() {
  return runTransaction("readwrite", (store, setResult, fail) => {
    const request = store.delete(RELATED_WORKS_HANDLE_KEY);
    request.onsuccess = () => {
      setResult(undefined);
    };
    request.onerror = () => {
      fail(request.error ?? new Error("Failed to clear stored file handle."));
    };
  });
}
