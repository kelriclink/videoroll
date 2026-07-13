export type RequestError = {
  message: string;
  status?: number;
};

export type SliceResult<T> =
  | { ok: true; value: T }
  | { ok: false; error: RequestError };

export type SliceLoadResult<Core, Optional extends Record<string, unknown>> = {
  core: SliceResult<Core>;
  optional: { [Key in keyof Optional]: SliceResult<Optional[Key]> };
  errors: {
    core: RequestError | null;
    optional: Partial<{ [Key in keyof Optional]: RequestError }>;
  };
};

export type DirtyFieldState<T> = {
  value: T;
  serverValue: T;
  serverVersion: string | number | null;
  dirty: boolean;
  conflict: { serverValue: T; serverVersion: string | number | null } | null;
};

function toRequestError(error: unknown): RequestError {
  if (error instanceof Error) {
    const status = typeof (error as Error & { status?: unknown }).status === "number"
      ? (error as Error & { status: number }).status
      : undefined;
    return { message: error.message, status };
  }
  return { message: String(error) };
}

async function settle<T>(loader: () => Promise<T>): Promise<SliceResult<T>> {
  try {
    return { ok: true, value: await loader() };
  } catch (error: unknown) {
    return { ok: false, error: toRequestError(error) };
  }
}

/**
 * Loads the task-critical snapshot independently from optional feature slices.
 * Consumers retain their existing values for any failed slice by applying only
 * successful results.
 */
export async function loadSlices<Core, Optional extends Record<string, unknown>>(
  coreLoader: () => Promise<Core>,
  optionalLoaders: { [Key in keyof Optional]: () => Promise<Optional[Key]> },
): Promise<SliceLoadResult<Core, Optional>> {
  const entries = Object.entries(optionalLoaders) as Array<[keyof Optional, () => Promise<Optional[keyof Optional]>]>;
  const [core, ...optionalResults] = await Promise.all([
    settle(coreLoader),
    ...entries.map(([, loader]) => settle(loader)),
  ]);
  const optional = {} as SliceLoadResult<Core, Optional>["optional"];
  const optionalErrors: SliceLoadResult<Core, Optional>["errors"]["optional"] = {};
  entries.forEach(([key], index) => {
    const result = optionalResults[index] as SliceResult<Optional[typeof key]>;
    (optional as unknown as Record<string, SliceResult<unknown>>)[String(key)] = result;
    if (!result.ok) (optionalErrors as Record<string, RequestError>)[String(key)] = result.error;
  });
  return {
    core: core as SliceResult<Core>,
    optional,
    errors: {
      core: core.ok ? null : core.error,
      optional: optionalErrors,
    },
  };
}

export function createDirtyFieldState<T>(value: T, serverVersion: string | number | null = null): DirtyFieldState<T> {
  return { value, serverValue: value, serverVersion, dirty: false, conflict: null };
}

export function editDirtyField<T>(state: DirtyFieldState<T>, value: T): DirtyFieldState<T> {
  return { ...state, value, dirty: true, conflict: null };
}

/** Do not replace a locally edited value during polling or a manual refresh. */
export function applyServerValue<T>(
  state: DirtyFieldState<T>,
  serverValue: T,
  serverVersion: string | number | null = null,
): DirtyFieldState<T> {
  if (!state.dirty) return createDirtyFieldState(serverValue, serverVersion);
  const versionChanged = serverVersion !== null && state.serverVersion !== null && serverVersion !== state.serverVersion;
  return {
    ...state,
    serverValue,
    serverVersion: serverVersion ?? state.serverVersion,
    conflict: versionChanged ? { serverValue, serverVersion } : state.conflict,
  };
}

export function markDirtyFieldSaved<T>(state: DirtyFieldState<T>, serverValue: T, serverVersion: string | number | null = state.serverVersion): DirtyFieldState<T> {
  return createDirtyFieldState(serverValue, serverVersion);
}

export function markDirtyFieldConflict<T>(
  state: DirtyFieldState<T>,
  serverValue: T,
  serverVersion: string | number | null = state.serverVersion,
): DirtyFieldState<T> {
  return { ...state, serverValue, serverVersion, conflict: { serverValue, serverVersion } };
}

export function reloadDirtyField<T>(state: DirtyFieldState<T>): DirtyFieldState<T> {
  return createDirtyFieldState(state.serverValue, state.serverVersion);
}
