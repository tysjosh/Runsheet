/** Jest configuration — `jest-expo` preset, so the React Native module graph is transformed. */

/**
 * Several packages this app imports publish their `react-native` entry point as
 * an untranspiled `.mjs` bundle — `lucide-react-native` (the icons in
 * `components/DispatchOrderCard.tsx`) and the `@rn-primitives/*` primitives the
 * copied `components/ui/` layer is built on. `jest-expo` transforms `.js`,
 * `.ts`, and `.tsx` only and ignores `node_modules` apart from its own
 * whitelist, so those bundles reach the runtime unparsed and any suite that
 * imports a primitive fails on `export`. Adding the `.mjs` transform and
 * widening the whitelist fixes both without touching the copied files.
 */
const transformIgnorePatterns = [
  'node_modules/(?!((jest-)?react-native|@react-native(-community)?)' +
    '|expo(nent)?|@expo(nent)?/.*|@expo-google-fonts/.*' +
    '|react-navigation|@react-navigation/.*' +
    '|@unimodules/.*|unimodules|sentry-expo|native-base' +
    '|react-native-svg|react-native-css-interop|nativewind' +
    '|lucide-react-native|@rn-primitives/.*)',
];

module.exports = {
  preset: 'jest-expo',
  testMatch: ['**/__tests__/**/*.test.ts', '**/__tests__/**/*.test.tsx'],
  collectCoverageFrom: ['lib/**/*.{ts,tsx}', 'components/**/*.{ts,tsx}'],
  moduleFileExtensions: ['ts', 'tsx', 'js', 'jsx', 'mjs', 'cjs', 'json', 'node'],
  transform: {
    '^.+\\.mjs$': 'babel-jest',
  },
  transformIgnorePatterns,
};
