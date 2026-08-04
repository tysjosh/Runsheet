import type { ConfigContext, ExpoConfig } from 'expo/config';

/**
 * Dynamic configuration layer over `app.json`.
 *
 * Every build credential and identifier is read from the environment (locally
 * from the shell, in CI and on EAS from EAS secrets) so that no account
 * identifier and no credential file is committed to this repository
 * (Requirements 16.25-16.29).
 *
 * Environment variables:
 *   RUNSHEET_IOS_BUNDLE_IDENTIFIER  iOS bundle identifier      (default below)
 *   RUNSHEET_ANDROID_PACKAGE        Android application id     (default below)
 *   RUNSHEET_EAS_OWNER              EAS account that owns the project
 *   RUNSHEET_EAS_PROJECT_ID         EAS project id
 *   GOOGLE_SERVICES_JSON            path to the Firebase `google-services.json`,
 *                                   supplied as an EAS *file* secret and never
 *                                   committed to this repository
 *
 * The defaults for the two identifiers sit under the Runsheet-controlled
 * reverse-domain namespace `com.runsheet.*`. Both are immutable once a build
 * carrying them is published to either store, so the Apple developer team, the
 * Google Play account, the Firebase project, and the EAS project must all be
 * provisioned under Runsheet ownership before the first build leaves the
 * development team (Requirement 16.30).
 */

const DEFAULT_IOS_BUNDLE_IDENTIFIER = 'com.runsheet.driver';
const DEFAULT_ANDROID_PACKAGE = 'com.runsheet.driver';

function nonEmpty(value: string | undefined): string | undefined {
  return value && value.trim().length > 0 ? value.trim() : undefined;
}

export default ({ config }: ConfigContext): ExpoConfig => {
  const iosBundleIdentifier =
    nonEmpty(process.env.RUNSHEET_IOS_BUNDLE_IDENTIFIER) ??
    DEFAULT_IOS_BUNDLE_IDENTIFIER;
  const androidPackage =
    nonEmpty(process.env.RUNSHEET_ANDROID_PACKAGE) ?? DEFAULT_ANDROID_PACKAGE;
  const easOwner = nonEmpty(process.env.RUNSHEET_EAS_OWNER);
  const easProjectId = nonEmpty(process.env.RUNSHEET_EAS_PROJECT_ID);
  const googleServicesFile = nonEmpty(process.env.GOOGLE_SERVICES_JSON);

  return {
    ...config,
    name: config.name ?? 'Runsheet Driver',
    slug: config.slug ?? 'runsheet-driver',
    ...(easOwner ? { owner: easOwner } : {}),
    ios: {
      ...config.ios,
      bundleIdentifier: iosBundleIdentifier,
    },
    android: {
      ...config.android,
      package: androidPackage,
      ...(googleServicesFile ? { googleServicesFile } : {}),
    },
    extra: {
      ...config.extra,
      ...(easProjectId ? { eas: { projectId: easProjectId } } : {}),
    },
  };
};
