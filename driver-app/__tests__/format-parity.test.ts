/**
 * Pins `lib/format.ts` (the driver port) to the staff app's
 * `runsheet/src/lib/format.ts` (UI revamp R13.9, D15: a port, not a shared
 * module). Every case has a literal expected string that BOTH implementations
 * must produce, so a rule change on either side fails here.
 *
 * The web module is read by path (the CI driver-app job checks out the whole
 * repo, as `tokens.test.ts` already relies on) and transpiled here with
 * TypeScript, so it needs neither the web app's node_modules nor a place in the
 * driver's tsc program.
 */
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import ts from 'typescript';
import * as driver from '@/lib/format';

type FormatModule = {
  date: typeof driver.date;
  dateLong: typeof driver.dateLong;
  time: typeof driver.time;
  dateTime: typeof driver.dateTime;
  relative: typeof driver.relative;
  humanize: typeof driver.humanize;
  productName: typeof driver.productName;
  // Web names it `window`; the driver port calls it `timeWindow`.
  window: typeof driver.timeWindow;
  EMPTY: string;
};
const WEB_SRC = join(__dirname, '..', '..', 'runsheet', 'src');

/** CommonJS-evaluate a web TS module; `deps` maps its import specifiers. */
function loadWebModule(file: string, deps: Record<string, unknown> = {}): Record<string, unknown> {
  const source = readFileSync(join(WEB_SRC, file), 'utf8');
  const { outputText } = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020 },
  });
  const module = { exports: {} as Record<string, unknown> };
  const requireDep = (specifier: string) => {
    if (!(specifier in deps)) throw new Error(`unexpected web import ${specifier} in ${file}`);
    return deps[specifier];
  };
  new Function('exports', 'require', 'module', outputText)(module.exports, requireDep, module);
  return module.exports;
}

const web = loadWebModule('lib/format.ts', {
  '../styles/tokens': loadWebModule('styles/tokens.ts'),
}) as unknown as FormatModule;

const opts = { timeZone: 'America/Chicago', locale: 'en-GB' };
const NOW = Date.parse('2026-10-08T15:00:00Z');

type Case = [label: string, run: (m: FormatModule) => string, expected: string];

const asDriver: FormatModule = { ...driver, window: driver.timeWindow };

const cases: Case[] = [
  ['date', (m) => m.date('2026-10-08T13:30:00Z', opts), 'Thu 8 Oct'],
  ['dateLong', (m) => m.dateLong('2026-10-08T13:30:00Z', opts), 'Thu 8 Oct 2026'],
  ['time 24 h, no seconds', (m) => m.time('2026-10-08T13:30:45Z', opts), '08:30'],
  ['time afternoon', (m) => m.time('2026-10-08T22:05:00Z', opts), '17:05'],
  ['dateTime', (m) => m.dateTime('2026-10-08T13:30:00Z', opts), 'Thu 8 Oct, 08:30'],
  ['window same day', (m) => m.window('2026-10-08T13:30:00Z', '2026-10-08T15:30:00Z', opts), '08:30–10:30'],
  [
    'window across days',
    (m) => m.window('2026-10-09T03:00:00Z', '2026-10-09T07:00:00Z', opts),
    'Thu 8 Oct, 22:00 – Fri 9 Oct, 02:00',
  ],
  ['window open end', (m) => m.window('2026-10-08T13:30:00Z', null, opts), 'from 08:30'],
  ['window open start', (m) => m.window(null, '2026-10-08T15:30:00Z', opts), 'until 10:30'],
  ['window empty', (m) => m.window(null, null, opts), '—'],
  ['invalid date', (m) => m.date('not a date', opts), '—'],
  ['missing time', (m) => m.time(undefined, opts), '—'],
  ['relative just now', (m) => m.relative(NOW - 20_000, { now: NOW, ...opts }), 'just now'],
  ['relative minutes ago', (m) => m.relative(NOW - 12 * 60_000, { now: NOW, ...opts }), '12 min ago'],
  ['relative future', (m) => m.relative(NOW + 20 * 60_000, { now: NOW, ...opts }), 'in 20 min'],
  ['relative hours', (m) => m.relative(NOW - 3 * 3_600_000, { now: NOW, ...opts }), '3 h ago'],
  ['relative beyond a day', (m) => m.relative(NOW - 3 * 86_400_000, { now: NOW, ...opts }), 'Mon 5 Oct'],
  ['humanize', (m) => m.humanize('kerosene_k1'), 'Kerosene k1'],
  ['productName known', (m) => m.productName('DIESEL_2'), 'Diesel #2 (on-road)'],
  ['productName unknown', (m) => m.productName('JET_A_PLUS'), 'Jet a plus'],
  ['productName missing', (m) => m.productName(null), '—'],
];

describe.each(cases)('format parity: %s', (_label, run, expected) => {
  it('web produces the pinned string', () => {
    expect(run(web)).toBe(expected);
  });
  it('the driver port produces the same string', () => {
    expect(run(asDriver)).toBe(expected);
  });
});

it('both use the same empty marker', () => {
  expect(driver.EMPTY).toBe(web.EMPTY);
});
