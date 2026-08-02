import { openURL } from 'expo-linking';
import { useState } from 'react';
import { KeyboardAvoidingView, Platform, View } from 'react-native';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Text } from '@/components/ui/text';
import { demoPreviewEnabled } from '@/lib/demo-preview';
import { notificationManager } from '@/lib/notification-manager';
import { signIn } from '@/lib/session';
import { forgotPasswordUrl } from '@/lib/web-app';
import { driverWebSocket } from '@/lib/websocket';

export default function SignInScreen() {
  const [email, setEmail] = useState(
    demoPreviewEnabled ? 'driver@demo.runsheet.app' : '',
  );
  const [password, setPassword] = useState(
    demoPreviewEnabled ? 'preview-only' : '',
  );
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  // Demo preview prefills a fake credential against a local fetch stub, so
  // there is no real account to recover; and with no configured web origin
  // there is nowhere to send the driver. Either way, render no affordance
  // rather than a link that cannot work.
  const resetUrl = demoPreviewEnabled ? null : forgotPasswordUrl();

  const openPasswordReset = async () => {
    if (!resetUrl) {
      return;
    }
    setMessage(null);
    try {
      // The system browser, not an in-app webview: the reset flow establishes a
      // SuperTokens session on the web origin and must not share this app's
      // webview state.
      await openURL(resetUrl);
    } catch {
      setMessage(
        'The password reset page could not be opened. Contact dispatch to have a reset link sent to you.',
      );
    }
  };

  const submit = async () => {
    if (!email.trim() || !password) {
      setMessage('Enter your driver email and password.');
      return;
    }
    setSubmitting(true);
    setMessage(null);
    try {
      await signIn({ email, password });
      if (!demoPreviewEnabled) {
        notificationManager.retry();
      }
      // Root startup initializes the channel once.  After an explicit sign-out
      // it is stopped, so initialize it again; the method is idempotent while
      // an existing session watcher is already active.
      if (!demoPreviewEnabled) {
        await driverWebSocket.initialize();
      }
    } catch (error) {
      setMessage(
        error instanceof Error
          ? error.message
          : 'The driver session could not be started.',
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <KeyboardAvoidingView
      className="flex-1 bg-background"
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <View className="flex-1 justify-center gap-8 px-6">
        <View className="gap-2">
          <Text className="text-4xl font-bold">Runsheet Driver</Text>
          <Text className="text-lg text-muted-foreground">
            {demoPreviewEnabled
              ? 'Open a local demo route with realistic fuel deliveries.'
              : 'Sign in to receive the routes your dispatcher approves.'}
          </Text>
        </View>

        <View className="gap-4">
          <View className="gap-2">
            <Text className="font-semibold">Email</Text>
            <Input
              value={email}
              onChangeText={setEmail}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="email-address"
              textContentType="username"
              placeholder="driver@company.com"
              editable={!submitting}
            />
          </View>
          <View className="gap-2">
            <Text className="font-semibold">Password</Text>
            <Input
              value={password}
              onChangeText={setPassword}
              secureTextEntry
              textContentType="password"
              placeholder="Password"
              editable={!submitting}
              onSubmitEditing={() => void submit()}
            />
          </View>
          {message && (
            <Text className="text-sm text-destructive">{message}</Text>
          )}
          <Button disabled={submitting} onPress={() => void submit()}>
            <Text>
              {submitting
                ? 'Signing in…'
                : demoPreviewEnabled
                  ? 'Open demo'
                  : 'Sign in'}
            </Text>
          </Button>
          {resetUrl && (
            <Button
              variant="link"
              size="sm"
              disabled={submitting}
              onPress={() => void openPasswordReset()}
            >
              <Text>Forgot your password?</Text>
            </Button>
          )}
        </View>
      </View>
    </KeyboardAvoidingView>
  );
}
