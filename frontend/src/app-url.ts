/// <reference types="vite/client" />

/** Resolve application-owned paths for both local root and production subpath builds. */
export function applicationUrl(path: string, base = import.meta.env?.BASE_URL || '/'): string {
  if (!path.startsWith('/') || path.startsWith('//')) return path;
  const prefix = base.replace(/\/$/, '');
  if (!prefix || path === prefix || path.startsWith(`${prefix}/`) || path.startsWith(`${prefix}?`))
    return path;
  return `${prefix}${path}`;
}
