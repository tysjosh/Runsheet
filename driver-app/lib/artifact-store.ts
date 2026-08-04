/**
 * On-device retention of POD artifact bytes (R11.16, R5.18).
 *
 * A signature raster, a delivery photo, and a meter-ticket photo are captured
 * before the POD submission is enqueued, and the submission may sit in the
 * offline queue for hours. The bytes therefore have to outlive the screen that
 * captured them, and they have to outlive the process:
 *
 *  - **Retained until acknowledged.** {@link putArtifact} writes the bytes into
 *    the document directory, which survives termination and device restart, and
 *    nothing deletes them while the owning queue row is still queued (R11.16).
 *  - **Deleted 24 hours after acknowledgement.** When the owning row leaves the
 *    queue with a 2xx, the queue calls {@link acknowledgeArtifacts}, which stamps
 *    a deletion deadline of *acknowledgement + 24 h*. {@link sweepArtifacts},
 *    run on app foreground, does the deleting. Both conditions must hold —
 *    acknowledgement alone does not delete, and the timer alone does not delete
 *    (R5.18).
 *  - **A failed row keeps its bytes.** {@link retainArtifactsIndefinitely} clears
 *    any deadline, so a POD that 422'd keeps its evidence until a dispatcher
 *    fixes the server-side cause and the driver retries.
 *  - **Orphans are swept.** Bytes with no metadata, metadata with no bytes, and
 *    bytes referenced by no queue row past a grace period are deleted.
 *  - **Gone at sign-out.** The eraser is registered against the `pod-artifacts`
 *    domain of `lib/session.ts` (R15.5).
 *
 * Bytes are held base64-encoded, which is the encoding `expo-image-manipulator`
 * produces and the encoding the presigned PUT consumes, so nothing is re-coded
 * on the retention path. No artifact byte is ever passed to a log statement.
 *
 * Requirements: 11.16, 5.18, 15.5
 */

import * as FileSystem from 'expo-file-system';

import { registerSessionPurgeHandler } from './session';

/** R5.18's deadline, measured from server acknowledgement. */
export const ARTIFACT_RETENTION_MS = 24 * 60 * 60 * 1000;

/**
 * How long an artifact that no queue row references is kept before it is treated
 * as an orphan. It covers the window between capture and enqueue — a driver who
 * photographs a meter ticket and then walks back to the cab must not lose it.
 */
export const ARTIFACT_ORPHAN_GRACE_MS = 24 * 60 * 60 * 1000;

const DIRECTORY_NAME = 'runsheet-pod-artifacts';
const BYTES_SUFFIX = '.bin';
const META_SUFFIX = '.json';

/**
 * The slice of `expo-file-system` this module needs. Injectable so the retention
 * rules can be exercised without a native module.
 */
export interface ArtifactFileSystem {
  documentDirectory: string | null;
  getInfoAsync(uri: string): Promise<{ exists: boolean; size?: number }>;
  makeDirectoryAsync(uri: string, options?: { intermediates?: boolean }): Promise<void>;
  readDirectoryAsync(uri: string): Promise<string[]>;
  readAsStringAsync(uri: string, options?: { encoding?: 'utf8' | 'base64' }): Promise<string>;
  writeAsStringAsync(
    uri: string,
    contents: string,
    options?: { encoding?: 'utf8' | 'base64' },
  ): Promise<void>;
  deleteAsync(uri: string, options?: { idempotent?: boolean }): Promise<void>;
}

/** What is known about one stored artifact. */
export interface ArtifactRecord {
  fileRef: string;
  contentType: string | null;
  /** Epoch ms at which the bytes were written. */
  storedAt: number;
  /** Epoch ms at which the owning mutation was acknowledged, or `null`. */
  acknowledgedAt: number | null;
  /** Epoch ms at or after which the bytes may be deleted, or `null` to keep. */
  deleteAfter: number | null;
  /** Size of the stored base64 payload in bytes, when the platform reports it. */
  size?: number;
}

interface StoredMeta {
  fileRef: string;
  contentType: string | null;
  storedAt: number;
  acknowledgedAt: number | null;
  deleteAfter: number | null;
}

/**
 * An in-memory stand-in used when there is no document directory — the web
 * preview and the Jest environment. Retention behaviour is identical; only
 * durability is lost, which is the safe direction to fail because it holds
 * *fewer* bytes on the device, never more.
 */
function createMemoryFileSystem(): ArtifactFileSystem {
  const files = new Map<string, string>();
  const directories = new Set<string>(['memory://']);
  const parentOf = (uri: string) => uri.slice(0, uri.lastIndexOf('/') + 1);

  return {
    documentDirectory: 'memory://',
    async getInfoAsync(uri) {
      const contents = files.get(uri);
      if (contents !== undefined) {
        return { exists: true, size: contents.length };
      }
      return { exists: directories.has(uri) };
    },
    async makeDirectoryAsync(uri) {
      directories.add(uri.endsWith('/') ? uri : `${uri}/`);
    },
    async readDirectoryAsync(uri) {
      const prefix = uri.endsWith('/') ? uri : `${uri}/`;
      return Array.from(files.keys())
        .filter((key) => key.startsWith(prefix))
        .map((key) => key.slice(prefix.length));
    },
    async readAsStringAsync(uri) {
      const contents = files.get(uri);
      if (contents === undefined) {
        throw new Error(`No artifact at ${uri}`);
      }
      return contents;
    },
    async writeAsStringAsync(uri, contents) {
      directories.add(parentOf(uri));
      files.set(uri, contents);
    },
    async deleteAsync(uri, options) {
      if (!files.delete(uri) && options?.idempotent !== true && !directories.delete(uri)) {
        throw new Error(`No artifact at ${uri}`);
      }
    },
  };
}

let fileSystem: ArtifactFileSystem | null = null;
let directoryOverride: string | null = null;
let clock: () => number = () => Date.now();
let ensuredDirectory: string | null = null;

function resolveFileSystem(): ArtifactFileSystem {
  if (!fileSystem) {
    fileSystem = FileSystem.documentDirectory
      ? (FileSystem as ArtifactFileSystem)
      : createMemoryFileSystem();
  }
  return fileSystem;
}

/** Override the file system, the directory, and the clock. Tests only. */
export function configureArtifactStore(next: {
  fileSystem?: ArtifactFileSystem | null;
  directory?: string | null;
  now?: (() => number) | null;
}): void {
  if (next.fileSystem !== undefined) {
    fileSystem = next.fileSystem;
    ensuredDirectory = null;
  }
  if (next.directory !== undefined) {
    directoryOverride = next.directory;
    ensuredDirectory = null;
  }
  if (next.now !== undefined) {
    clock = next.now ?? (() => Date.now());
  }
}

/** The directory artifact bytes live in, with a trailing slash. */
export function artifactDirectory(): string {
  if (directoryOverride) {
    return directoryOverride.endsWith('/') ? directoryOverride : `${directoryOverride}/`;
  }
  const root = resolveFileSystem().documentDirectory ?? 'memory://';
  return `${root.endsWith('/') ? root : `${root}/`}${DIRECTORY_NAME}/`;
}

async function ensureDirectory(): Promise<string> {
  const directory = artifactDirectory();
  if (ensuredDirectory === directory) {
    return directory;
  }
  const fs = resolveFileSystem();
  const info = await fs.getInfoAsync(directory).catch(() => ({ exists: false }));
  if (!info.exists) {
    await fs.makeDirectoryAsync(directory, { intermediates: true }).catch(() => undefined);
  }
  ensuredDirectory = directory;
  return directory;
}

/**
 * A `file_ref` is server-issued and tenant-prefixed, so it contains path
 * separators. Percent-encoding it yields one flat, reversible file name.
 */
function encodeRef(fileRef: string): string {
  return encodeURIComponent(fileRef);
}

function decodeRef(name: string): string | null {
  try {
    return decodeURIComponent(name);
  } catch {
    return null;
  }
}

function bytesUriFor(directory: string, fileRef: string): string {
  return `${directory}${encodeRef(fileRef)}${BYTES_SUFFIX}`;
}

function metaUriFor(directory: string, fileRef: string): string {
  return `${directory}${encodeRef(fileRef)}${META_SUFFIX}`;
}

async function readMeta(directory: string, fileRef: string): Promise<StoredMeta | null> {
  const raw = await resolveFileSystem()
    .readAsStringAsync(metaUriFor(directory, fileRef), { encoding: 'utf8' })
    .catch(() => null);
  if (!raw) {
    return null;
  }
  try {
    const parsed = JSON.parse(raw) as Partial<StoredMeta>;
    if (typeof parsed.fileRef !== 'string') {
      return null;
    }
    return {
      fileRef: parsed.fileRef,
      contentType: typeof parsed.contentType === 'string' ? parsed.contentType : null,
      storedAt: typeof parsed.storedAt === 'number' ? parsed.storedAt : 0,
      acknowledgedAt: typeof parsed.acknowledgedAt === 'number' ? parsed.acknowledgedAt : null,
      deleteAfter: typeof parsed.deleteAfter === 'number' ? parsed.deleteAfter : null,
    };
  } catch {
    return null;
  }
}

async function writeMeta(directory: string, meta: StoredMeta): Promise<void> {
  await resolveFileSystem().writeAsStringAsync(
    metaUriFor(directory, meta.fileRef),
    JSON.stringify(meta),
    { encoding: 'utf8' },
  );
}

async function deleteQuietly(uri: string): Promise<void> {
  await resolveFileSystem().deleteAsync(uri, { idempotent: true }).catch(() => undefined);
}

// ---------------------------------------------------------------------------
// Public surface
// ---------------------------------------------------------------------------

/**
 * Write artifact bytes under a `file_ref`.
 *
 * The bytes are retained with no deletion deadline: only an acknowledgement
 * through {@link acknowledgeArtifacts} can set one (R11.16).
 *
 * @param base64 the artifact payload, base64-encoded.
 * @returns the on-device URI of the stored bytes.
 */
export async function putArtifact(args: {
  fileRef: string;
  base64: string;
  contentType?: string | null;
}): Promise<string> {
  const directory = await ensureDirectory();
  const bytesUri = bytesUriFor(directory, args.fileRef);
  await resolveFileSystem().writeAsStringAsync(bytesUri, args.base64, { encoding: 'base64' });
  await writeMeta(directory, {
    fileRef: args.fileRef,
    contentType: args.contentType ?? null,
    storedAt: clock(),
    acknowledgedAt: null,
    deleteAfter: null,
  });
  return bytesUri;
}

/** The on-device URI of an artifact's bytes, whether or not it exists. */
export function artifactUri(fileRef: string): string {
  return bytesUriFor(artifactDirectory(), fileRef);
}

/** Read artifact bytes back as base64, or `null` when they are gone. */
export async function readArtifact(fileRef: string): Promise<string | null> {
  const directory = artifactDirectory();
  return resolveFileSystem()
    .readAsStringAsync(bytesUriFor(directory, fileRef), { encoding: 'base64' })
    .catch(() => null);
}

/** Whether the bytes for a `file_ref` are still on the device. */
export async function hasArtifact(fileRef: string): Promise<boolean> {
  const info = await resolveFileSystem()
    .getInfoAsync(bytesUriFor(artifactDirectory(), fileRef))
    .catch(() => ({ exists: false }));
  return info.exists === true;
}

/** Everything currently retained, newest deadline included. */
export async function listArtifacts(): Promise<ArtifactRecord[]> {
  const directory = await ensureDirectory();
  const fs = resolveFileSystem();
  const names = await fs.readDirectoryAsync(directory).catch(() => [] as string[]);
  const records: ArtifactRecord[] = [];
  for (const name of names) {
    if (!name.endsWith(META_SUFFIX)) {
      continue;
    }
    const fileRef = decodeRef(name.slice(0, -META_SUFFIX.length));
    if (!fileRef) {
      continue;
    }
    const meta = await readMeta(directory, fileRef);
    if (!meta) {
      continue;
    }
    const info = await fs
      .getInfoAsync(bytesUriFor(directory, fileRef))
      .catch((): { exists: boolean; size?: number } => ({ exists: false }));
    if (!info.exists) {
      continue;
    }
    records.push({ ...meta, size: info.size });
  }
  return records;
}

/**
 * Record that the mutation carrying these refs was acknowledged with a 2xx, and
 * schedule deletion 24 hours later (R5.18).
 *
 * Deletion is *scheduled*, not performed: R5.18 requires both the
 * acknowledgement and the elapsed timer, whichever is later.
 */
export async function acknowledgeArtifacts(
  fileRefs: Iterable<string>,
  options: { retentionMs?: number } = {},
): Promise<void> {
  const retentionMs = options.retentionMs ?? ARTIFACT_RETENTION_MS;
  const directory = await ensureDirectory();
  const acknowledgedAt = clock();
  for (const fileRef of fileRefs) {
    const meta = await readMeta(directory, fileRef);
    if (!meta) {
      continue;
    }
    await writeMeta(directory, {
      ...meta,
      acknowledgedAt,
      deleteAfter: acknowledgedAt + retentionMs,
    });
  }
}

/**
 * Clear any deletion deadline, so the bytes are kept until something else
 * deletes them. Used for a `failed` queue row: losing delivery evidence because
 * a request 422'd would be the worst possible outcome of a validation bug.
 */
export async function retainArtifactsIndefinitely(fileRefs: Iterable<string>): Promise<void> {
  const directory = await ensureDirectory();
  for (const fileRef of fileRefs) {
    const meta = await readMeta(directory, fileRef);
    if (!meta || meta.deleteAfter === null) {
      continue;
    }
    await writeMeta(directory, { ...meta, deleteAfter: null });
  }
}

/** Delete one artifact's bytes and metadata, whatever its retention state. */
export async function deleteArtifact(fileRef: string): Promise<void> {
  const directory = artifactDirectory();
  await deleteQuietly(bytesUriFor(directory, fileRef));
  await deleteQuietly(metaUriFor(directory, fileRef));
}

/**
 * Foreground sweep.
 *
 * Deletes, in order of certainty:
 *  1. artifacts whose deletion deadline has passed (acknowledged ≥ 24 h ago);
 *  2. bytes with no metadata and metadata with no bytes — a half-written pair;
 *  3. artifacts no queue row references, once past {@link ARTIFACT_ORPHAN_GRACE_MS}.
 *
 * Pass `referencedRefs` — the union of every queued row's `artifact_refs` — to
 * enable the third rule. Omitting it disables orphan collection, which is the
 * conservative default.
 *
 * @returns the number of artifacts deleted.
 */
export async function sweepArtifacts(
  options: {
    referencedRefs?: Iterable<string>;
    orphanGraceMs?: number;
  } = {},
): Promise<number> {
  const directory = await ensureDirectory();
  const fs = resolveFileSystem();
  const now = clock();
  const orphanGraceMs = options.orphanGraceMs ?? ARTIFACT_ORPHAN_GRACE_MS;
  const referenced = options.referencedRefs ? new Set(options.referencedRefs) : null;

  const names = await fs.readDirectoryAsync(directory).catch(() => [] as string[]);
  const metaRefs = new Set<string>();
  const byteRefs = new Set<string>();
  for (const name of names) {
    if (name.endsWith(META_SUFFIX)) {
      const ref = decodeRef(name.slice(0, -META_SUFFIX.length));
      if (ref) {
        metaRefs.add(ref);
      }
    } else if (name.endsWith(BYTES_SUFFIX)) {
      const ref = decodeRef(name.slice(0, -BYTES_SUFFIX.length));
      if (ref) {
        byteRefs.add(ref);
      }
    }
  }

  let deleted = 0;

  // Rule 2 — a half-written pair is unusable in either direction.
  for (const ref of byteRefs) {
    if (!metaRefs.has(ref)) {
      await deleteArtifact(ref);
      deleted += 1;
    }
  }
  for (const ref of metaRefs) {
    if (!byteRefs.has(ref)) {
      await deleteArtifact(ref);
      deleted += 1;
      metaRefs.delete(ref);
    }
  }

  for (const ref of metaRefs) {
    const meta = await readMeta(directory, ref);
    if (!meta) {
      // Unparseable metadata: the bytes can never be attributed to a POD.
      await deleteArtifact(ref);
      deleted += 1;
      continue;
    }
    // Rule 1 — acknowledged and the 24-hour timer has elapsed (R5.18).
    if (meta.deleteAfter !== null && now >= meta.deleteAfter) {
      await deleteArtifact(ref);
      deleted += 1;
      continue;
    }
    // Rule 3 — nothing in the queue will ever upload these bytes.
    if (referenced && !referenced.has(ref) && now - meta.storedAt >= orphanGraceMs) {
      await deleteArtifact(ref);
      deleted += 1;
    }
  }

  return deleted;
}

/** Delete every retained artifact. Registered as the sign-out eraser (R15.5). */
export async function purgeArtifacts(): Promise<void> {
  const directory = artifactDirectory();
  const names = await resolveFileSystem()
    .readDirectoryAsync(directory)
    .catch(() => [] as string[]);
  for (const name of names) {
    await deleteQuietly(`${directory}${name}`);
  }
}

registerSessionPurgeHandler('pod-artifacts', purgeArtifacts);
