import { useState } from 'react';
import { KeyboardAvoidingView, Platform, View } from 'react-native';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Text } from '@/components/ui/text';
import { demoPreviewEnabled } from '@/lib/demo-preview';
import { notificationManager } from '@/lib/notification-manager';
import { signIn } from '@/lib/session';
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
        </View>
      </View>
    </KeyboardAvoidingView>
  );
}
