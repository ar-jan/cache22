# Requirements

## Storage format

MUST:
- Avoid many small files:
  * inode explosion/exhaustion
  * slow copying due to file overhead, disk I/O
- Store complete Git history
- Recreate git repository with original commit hashes
- Incremental updates

So: avoid standard Git repositories.

Options:
- Git bundle
- Fossil SCM
- Compressed archive

Fossil SCM looks promising:
- `fossil import --git`
- `fossil git export`
- `--incremental` import option

## Features

### CLI

- Add single repository URL
- Add text to be parsed for supported URLs
- Default: add to index
- Commands:
  * list indexed repositories
  * list downloaded repositories
  * add URL
  * add and download
  * queue for download
  * delete
  * check for updates
  * fetch incremental updates
  * compare local index with on-disk

### GUI

Expose the CLI features in an helpful GUI.

- Server with web interface, or native app?

## Implementation

Decide on tech stack for CLI-GUI combo.
