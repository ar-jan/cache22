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
