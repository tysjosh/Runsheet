"use client";

import { useRouter } from "next/navigation";
import SignIn from "../../components/SignIn";

export default function SignInPage() {
  const router = useRouter();

  const handleSignIn = (email: string, password: string) => {
    // Mock authentication - in real app, this would call an API
    if (email === "admin@runsheet.com" && password === "demo123") {
      // Store auth state in sessionStorage (cleared on tab close)
      // In production, use httpOnly cookies set by the backend
      sessionStorage.setItem("isAuthenticated", "true");

      // Use replace instead of push to prevent back navigation to signin
      router.replace("/dashboard");
    } else {
      // This error will be handled by the SignIn component
      throw new Error("Invalid credentials");
    }
  };

  return <SignIn onSignIn={handleSignIn} />;
}
