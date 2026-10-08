import { Stack, useRouter } from 'expo-router';
import { View } from 'react-native';

import { Button } from '@/components/ui/button';
import { Text } from '@/components/ui/text';

export default function NotFoundScreen() {
  const router = useRouter();
  return (
    <>
      <Stack.Screen options={{ title: 'Not found' }} />
      <View className="flex-1 items-center justify-center gap-5 bg-background p-5">
        <Text className="text-center text-xl font-semibold">
          This screen does not exist.
        </Text>
        <Button onPress={() => router.replace('/')}>
          <Text>Back to today&apos;s work</Text>
        </Button>
      </View>
    </>
  );
}
