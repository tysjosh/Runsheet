const { getDefaultConfig } = require('expo/metro-config');
const { withNativeWind } = require('nativewind/metro');

const config = getDefaultConfig(__dirname);

// expo-sqlite's browser worker imports its SQLite runtime as a WebAssembly
// asset. Metro does not include `.wasm` in every Expo SDK resolver profile, so
// register it explicitly for the local/web preview while leaving native
// resolution unchanged.
if (!config.resolver.assetExts.includes('wasm')) {
  config.resolver.assetExts.push('wasm');
}

module.exports = withNativeWind(config, { input: './global.css' });
