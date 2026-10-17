import assert from 'node:assert/strict';
import { createServer } from 'vite';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import type { WorkspaceEvent } from '../src/types';

const base = '/manufacturing-quality/';
const origin = 'https://tranthiennhan.com';
Object.defineProperty(globalThis, 'window', {
  value: { location: { origin, href: `${origin}${base}`, protocol: 'https:' } },
});
const requested: string[] = [];
globalThis.fetch = async (input) => {
  requested.push(String(input));
  return new Response('{}', { headers: { 'Content-Type': 'application/json' } });
};

// Load the real modules through Vite so BASE_URL matches the production build.
const server = await createServer({
  configFile: false,
  base,
  server: { middlewareMode: true, hmr: false },
  appType: 'custom',
});
try {
  const { applicationUrl } = await server.ssrLoadModule('/src/app-url.ts');
  const api = await server.ssrLoadModule('/src/api.ts');
  const operations = await server.ssrLoadModule('/src/operations-api.ts');
  const { createI18n, I18nProvider } = await server.ssrLoadModule('/src/i18n.tsx');
  const { default: EventView } = await server.ssrLoadModule('/src/EventView.tsx');
  const { projectConversation } = await server.ssrLoadModule('/src/AgentWorkspace.tsx');
  assert.equal(applicationUrl('/api/config'), `${base}api/config`);
  assert.equal(applicationUrl(`${base}api/config`), `${base}api/config`);
  assert.equal(applicationUrl('#dashboard-content'), '#dashboard-content');
  assert.equal(applicationUrl('https://example.org/source'), 'https://example.org/source');
  assert.equal(applicationUrl('/api/config', '/'), '/api/config');
  await api.getConfig();
  await api.getRecords('/api/dashboard/contexts/frozen/records', 1, 'defect_date', 'asc');
  await operations.listOperationCases();
  assert.deepEqual(requested, [
    `${base}api/config`,
    `${base}api/dashboard/contexts/frozen/records?page=1&page_size=20&sort_by=defect_date&sort_order=asc`,
    `${base}api/operations/cases`,
  ]);
  assert.equal(
    api.socketUrl('session', 42),
    `wss://tranthiennhan.com${base}api/sessions/session/ws?after=42`,
  );
  assert.equal(
    createI18n('vi').download('/api/dashboard/contexts/frozen/export.csv'),
    `${base}api/dashboard/contexts/frozen/export.csv?locale=vi`,
  );
  assert.equal(api.artifactUrl('https://external.example/report.csv'), undefined);
  assert.equal(api.artifactUrl('javascript:alert(1)'), undefined);

  const item = {
    path: 'artifacts/Chất lượng 50% #1.png',
    name: 'Chất lượng 50% #1.png',
    sha256: 'a'.repeat(64),
    size_bytes: 240,
    mime_type: 'image/png',
    url: '/api/sessions/session/runs/run/artifacts/artifacts/Ch%E1%BA%A5t%20l%C6%B0%E1%BB%A3ng%2050%25%20%231.png',
  };
  let eventId = 0;
  const event = (type: string, data: Record<string, unknown>): WorkspaceEvent => ({
    id: ++eventId,
    type,
    data,
    session_id: 'session',
    run_id: 'run',
    time: '2026-10-17T00:00:00Z',
  });
  const selected = (url: string) => {
    const artifact = { ...item, url };
    return projectConversation([
      event('run_started', { artifact_delivery_version: 1 }),
      event('artifact', { ...artifact, size: 240, mime: 'image/png', delivery_candidate: true }),
      event('message', {
        role: 'assistant',
        text: 'Selected file.',
        qa: { verified: true },
        delivery: {
          version: 1,
          artifacts: [artifact],
          source_execution_ids: ['receipt'],
          scope: null,
        },
      }),
    ]).flatMap((entry: WorkspaceEvent) =>
      entry.type === 'artifact_group' ? entry.data.items : entry.type === 'artifact' ? [entry] : [],
    );
  };
  assert.equal(
    selected(item.url).length,
    1,
    'A canonical chart source preserves owning-path selection under the prefix',
  );
  assert.equal(
    selected(`${base.slice(0, -1)}${item.url}`).length,
    1,
    'Already prefixed links are not prefixed twice',
  );
  assert.equal(selected(item.url.replace('/runs/run/', '/runs/foreign/')).length, 0);
  assert.equal(selected(item.url.replace('/sessions/session/', '/sessions/foreign/')).length, 0);
  assert.equal(
    selected(item.url.replace('/artifacts/artifacts/', '/artifacts/artifacts/%2e%2e/')).length,
    0,
  );
  assert.equal(selected(`${item.url}?download=1`).length, 0);
  assert.equal(selected(`${item.url}#fragment`).length, 0);
  assert.equal(selected(`https://external.example${item.url}`).length, 0);
  const encodedUrl = api.artifactUrl(item.url);
  const markup = renderToStaticMarkup(
    React.createElement(
      I18nProvider,
      { initialLocale: 'en', persist: false },
      React.createElement(EventView, {
        event: event('message', {
          role: 'assistant',
          text: `![Selected chart](${item.url})\n\n[Old download](${item.url})\n\n[Source](https://example.org/publication)`,
          qa: { verified: true },
          chart_urls: [encodedUrl],
        }),
      }),
    ),
  );
  assert.ok(markup.includes(`src="${encodedUrl}"`), 'Chart sources use the production subpath');
  assert.ok(
    markup.includes('href="https://example.org/publication"'),
    'External citations retain their source URL',
  );
  console.log(
    'Verified production subpath requests, WebSockets, Dashboard download routes, Unicode chart sources, exact owning paths, and local root compatibility.',
  );
} finally {
  await server.close();
}
