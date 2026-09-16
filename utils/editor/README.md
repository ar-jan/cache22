# Related Works Editor

This is a temporary / experimental basic UI for editing lists of related works stored in YAML format.

```sh
# Run Vite dev server, then open:
# http://127.0.0.1:5173/utils/editor/
npm run editor:dev
# Check yaml:
uv run --group dev yamllint docs/related-works.yaml
```

If the editor shows `File Access: Not supported in this browser` in Brave, enable `brave://flags/#file-system-access-api` and relaunch the browser.

Use `Save In Place` to write changes back to disk. On first save it will ask you to pick the target YAML file (choose `docs/related-works.yaml`), then it will reuse that file handle for later saves.
The selected file handle is persisted in browser storage; if permission is still granted, the editor auto-loads that same local file on next page load.
