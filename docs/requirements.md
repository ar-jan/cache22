# Requirements

## Avoid many small files

- inode explosion/exhaustion
- slow copying due to file overhead, disk I/O

So: avoid standard Git repositories as storage format.

Options:
- Git bundle
- Fossil SCM
- Compressed archive
- ...
