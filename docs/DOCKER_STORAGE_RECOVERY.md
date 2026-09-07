# Docker storage move and recovery

This guide separates the active Incident Resolution AI data from unrelated
Docker data that may be archived on the external `T7 Shield` drive.

## Current storage state

- Docker's VM disk is at
  `~/Library/Containers/com.docker.docker/Data/vms/0/data/Docker.raw`.
- The file has a 64 GB logical capacity and currently occupies approximately
  34.8 GB of physical space on the Mac.
- The T7 Shield is ExFAT and has approximately 946 GB free.
- Docker currently reports `overlay2` input/output errors. Because of that
  corruption, Docker cannot provide trustworthy per-image, build-cache, or
  volume sizes. Do not delete originals until an exported backup has been
  verified.

## Keep in Docker

The following data belongs to this repository and must remain available:

- Container `incident-resolution-ai-neo4j-1`.
- Named volume `incident-resolution-ai_neo4j_data`.
- The anonymous logs volume attached to the Neo4j container.
- The repository's `./data` directory, which Compose bind-mounts at `/app/data`.
- Images and build layers needed by the current `docker-compose.yml` until the
  stack has been tested after cleanup.

Never run `docker compose down --volumes` in this repository unless the Neo4j
database has been backed up and intentional deletion has been confirmed.

## Candidates to archive on the T7 Shield

These are unrelated to this repository, but may contain important application
or database data:

- `n8n_data`
- `opencve-docker_postgres`
- The data used by `mysql-container`, after its mount is identifiable
- Other anonymous volumes, only after their owning container and contents have
  been identified

Exporting a volume does not free Mac storage. Space is recovered only after the
export is verified and the original Docker volume is deliberately removed.

## Candidates to recreate instead of archive

- Unused build cache can be rebuilt by a future `docker build` or
  `docker compose build`.
- Unused images can be pulled or rebuilt from Dockerfiles and Compose files.
- The anonymous `node_modules` volume attached to
  `ui-evaluation-tool-react-ui-1` can be recreated with the project's package
  manager. Docker will not delete it while that stopped container still
  references it.

## Expected space recovery

Docker currently occupies approximately 34.8 GB on the Mac. Exact selective
recovery cannot be calculated while Docker's storage index returns I/O errors.

- Exporting backups to the T7 without deleting originals: **0 GB freed**.
- Removing unused cache/images: **unknown until Docker's index is repaired**.
- Moving the complete Docker VM disk to a suitable APFS destination:
  **approximately 34.8 GB freed on the Mac**.
- Deleting verified, unrelated volumes after export: their measured physical
  sizes, which Docker cannot currently report reliably.

The T7 Shield is currently ExFAT. Use it for `.tar` backup files, but do not use
it as Docker's live disk-image location. A separate APFS volume or partition is
recommended for live Docker storage.

## Suggested T7 backup layout

```text
/Volumes/T7 Shield/Docker-Backups/
├── full-vm/
├── volumes/
│   ├── n8n/
│   ├── opencve-postgres/
│   └── mysql/
├── images/
└── manifests/
```

Store a copy of the relevant Compose files and a text inventory of containers,
images, and volumes in `manifests/`. Never place `.env` files or credentials in
an unencrypted backup.

## Export and restore an individual volume

Prefer Docker Desktop's **Volumes** view while the daemon is healthy:

1. Open Docker Desktop and select **Volumes**.
2. Select the unrelated named volume.
3. Confirm its owning container and inspect its stored data.
4. Use **Export** and choose a path under
   `/Volumes/T7 Shield/Docker-Backups/volumes/`.
5. Confirm that the export completed, has a non-zero size, and can be opened or
   imported before deleting the original volume.

To restore later:

1. Open Docker Desktop and select **Volumes**.
2. Create a volume using the original name.
3. Open the volume and use **Import** to select its backup on the T7.
4. Recreate the service with its original Compose file.
5. Start the service and verify its data before deleting the backup.

Database-consistent exports made by Neo4j, PostgreSQL, or MySQL tools are safer
than copying a live database volume. Stop writes or use the database's supported
backup command before exporting database storage.

## Back up and restore the complete Docker VM

Use this only as a safety copy or when repairing/reinstalling Docker Desktop.

1. Fully quit Docker Desktop.
2. Confirm that Docker Desktop and its VM processes have stopped.
3. Copy `Docker.raw` to
   `/Volumes/T7 Shield/Docker-Backups/full-vm/Docker.raw`.
4. Verify the copied file before resetting, reinstalling, or deleting anything.

To restore later:

1. Fully quit Docker Desktop.
2. Restore the backed-up `Docker.raw` to Docker Desktop's expected data path.
3. Start Docker Desktop.
4. Verify containers and volumes before removing the T7 backup.

Restoring `Docker.raw` replaces the Docker environment as a whole; it is not a
selective volume restore. Keep a second copy of important database exports when
possible.

## Recreate disposable Docker data

From a project containing a valid Compose file:

```bash
docker compose pull
docker compose build
docker compose up -d
```

For a Node.js project whose anonymous `node_modules` volume was removed, use the
project's lockfile and package manager, for example:

```bash
npm ci
docker compose up -d --build
```

Before any cleanup, verify the active Incident Resolution AI stack:

```bash
cd /Users/soumitri/Desktop/incident-resolution-ai
docker compose config
docker compose ps
```

After cleanup or restoration, start it and check its logs:

```bash
docker compose up -d
docker compose ps
docker compose logs --tail=100 neo4j app
```
