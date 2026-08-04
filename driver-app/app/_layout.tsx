/**
 * TanStack Query configuration extracted from azumi-rider/app/_layout.tsx
 * Copied: 2026-07-29
 * Donor: azumi-rider (Expo SDK 53). Requirement 16.2 names "the TanStack Query
 * configuration extracted from" the donor layout, not the layout file, so what
 * is carried is exactly the `QueryClient` defaults, the `onlineManager` bound to
 * `@react-native-community/netinfo`, and the `focusManager` bound to `AppState`
 * (Requirements 16.3, 16.4).
 *
 * Not carried: the donor layout's `better-auth` provider (Requirement 16.11),
 * its `/rider/*` query and mutation clients (Requirement 16.12), its wallet and
 * earnings invalidations (Requirement 16.6), and its food-delivery notification
 * channel, categories, and deep links. Those arrive fresh in tasks 18.6 to 18.8.
 *
 * The `onlineManager` listener is installed once at module scope rather than on
 * every render, which is the one behavioural fix to the donor's placement.
 */

import { DarkTheme, DefaultTheme, ThemeProvider } from '@react-navigation/native';
import NetInfo from '@react-native-community/netinfo';
import { QueryClient, QueryClientProvider, focusManager, onlineManager } from '@tanstack/react-query';
import { useFonts } from 'expo-font';
import { Stack, useRouter, useSegments } from 'expo-router';
import { StatusBar } from 'expo-status-bar';
import { useEffect, useState } from 'react';
import { AppState, Platform, type AppStateStatus } from 'react-native';
import 'react-native-reanimated';
import '../global.css';

import { useColorScheme } from '@/hooks/useColorScheme';
import {
  demoPreviewEnabled,
  installDemoPreview,
} from '@/lib/demo-preview';
import { adoptServerDutyStatus, loadDriverIdentity } from '@/lib/duty-api';
import { locationTracker } from '@/lib/location-tracker';
import { drainQueue, initializeQueue } from '@/lib/offline-queue';
import { notificationManager } from '@/lib/notification-manager';
import { syncPendingPodCaptures } from '@/lib/pod-api';
import {
  initializeSession,
  subscribeToSession,
  type SessionIdentity,
} from '@/lib/session';
import { driverWebSocket } from '@/lib/websocket';

/** Query defaults carried from the donor layout verbatim. */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5 * 60 * 1000, // 5 minutes
      gcTime: 10 * 60 * 1000, // 10 minutes
      retry: 2,
      refetchOnWindowFocus: true,
    },
    mutations: {
      retry: 1,
    },
  },
});

/**
 * Network-state-driven online manager (R16.4). Queries pause while the device
 * is offline and resume on reconnection, which is what keeps the work list from
 * hammering a dead radio in a tunnel.
 */
onlineManager.setEventListener((setOnline) =>
  NetInfo.addEventListener((state) => {
    setOnline(!!state.isConnected);
  })
);

/** Application-state-driven focus manager (R16.4). */
function onAppStateChange(status: AppStateStatus) {
  if (Platform.OS !== 'web') {
    focusManager.setFocused(status === 'active');
  }
}

export default function RootLayout() {
  const colorScheme = useColorScheme();
  const router = useRouter();
  const segments = useSegments();
  const [sessionReady, setSessionReady] = useState(false);
  const [identity, setIdentity] = useState<SessionIdentity | null>(null);
  const [loaded] = useFonts({
    SpaceMono: require('../assets/fonts/SpaceMono-Regular.ttf'),
  });

  useEffect(() => {
    const subscription = AppState.addEventListener('change', onAppStateChange);
    return () => subscription.remove();
  }, []);

  useEffect(() => {
    const unsubscribeSession = subscribeToSession(setIdentity);
    void (async () => {
      installDemoPreview();
      const restoredIdentity = await initializeSession();
      setIdentity(restoredIdentity);
      setSessionReady(true);
      if (!demoPreviewEnabled) {
        notificationManager.start({ queryClient });
        driverWebSocket.setQueryClient(queryClient);
        await driverWebSocket.initialize();
      }
      await initializeQueue();
      await syncPendingPodCaptures();
      await drainQueue();
      if (restoredIdentity) {
        // R13.10 — the server's duty status wins over whatever this device
        // stored. A failed read leaves the stored value in place; it is a cache
        // of a server fact, not a competing one.
        await loadDriverIdentity()
          .then((identity) => adoptServerDutyStatus(identity.duty_status))
          .catch(() => undefined);
        // R10.9, R10.10 — sampling follows the adopted duty status, and the
        // buffered track drains on reconnection (R10.12).
        await locationTracker.initialize();
      }
    })().catch(() => {
      // The screens surface configuration/session errors. Bootstrap must not
      // produce an unhandled rejection that crashes the native shell.
    });
    const unsubscribe = NetInfo.addEventListener((state) => {
      if (state.isConnected) {
        void (async () => {
          await syncPendingPodCaptures();
          await drainQueue();
        })().catch(() => {
          // The durable draft/queue rows remain available for the next pass.
        });
      }
    });
    return () => {
      unsubscribe();
      unsubscribeSession();
      driverWebSocket.disconnect();
      void locationTracker.shutdown().catch(() => undefined);
    };
  }, []);

  useEffect(() => {
    if (!sessionReady) {
      return;
    }
    const onSignInScreen = segments[0] === 'sign-in';
    if (!identity && !onSignInScreen) {
      router.replace('/sign-in');
    } else if (identity && onSignInScreen) {
      router.replace('/(tabs)');
    }
  }, [identity, router, segments, sessionReady]);

  if (!loaded || !sessionReady) {
    // Async font loading only occurs in development.
    return null;
  }

  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider value={colorScheme === 'dark' ? DarkTheme : DefaultTheme}>
        <Stack>
          <Stack.Screen name="sign-in" options={{ headerShown: false }} />
          <Stack.Screen name="(tabs)" options={{ headerShown: false }} />
          <Stack.Screen name="order/[orderId]" options={{ title: 'Delivery' }} />
          <Stack.Screen name="order/[orderId]/pod" options={{ title: 'Proof of delivery' }} />
          <Stack.Screen
            name="order/[orderId]/exception"
            options={{ title: 'Report a problem' }}
          />
          <Stack.Screen name="+not-found" />
        </Stack>
        <StatusBar style="auto" />
      </ThemeProvider>
    </QueryClientProvider>
  );
}
