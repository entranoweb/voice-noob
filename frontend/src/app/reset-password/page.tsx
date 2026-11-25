"use client";

import { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Loader2 } from "lucide-react";

const API_BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";
const MIN_PASSWORD_LENGTH = 8;

function ResetPasswordForm() {
  const params = useSearchParams();
  // Read once, then keep it in state and take it out of the address bar: the
  // link is a credential, and a URL ends up in history and in screenshots.
  const [token] = useState(() => params.get("token") ?? "");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [done, setDone] = useState(false);
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    if (token) window.history.replaceState(null, "", window.location.pathname);
  }, [token]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    if (password !== confirm) {
      setError("Passwords do not match");
      return;
    }
    if (password.length < MIN_PASSWORD_LENGTH) {
      setError(`Password must be at least ${MIN_PASSWORD_LENGTH} characters`);
      return;
    }
    setIsLoading(true);
    try {
      const response = await fetch(`${API_BASE}/api/v1/auth/reset-password`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, new_password: password }),
      });
      if (response.ok) {
        setDone(true);
      } else if (response.status === 400) {
        setError("This reset link is invalid or has expired. Ask for a new one.");
      } else {
        setError("Could not reset the password. Try again.");
      }
    } catch {
      setError("Could not reach the server. Try again.");
    } finally {
      setIsLoading(false);
    }
  };

  if (!token) {
    return (
      <CardContent className="space-y-4">
        <p role="alert" className="text-sm">
          This page needs the link from the reset email.
        </p>
        <Link href="/forgot-password" className="text-sm text-primary hover:underline">
          Ask for a new link
        </Link>
      </CardContent>
    );
  }

  if (done) {
    return (
      <CardContent className="space-y-4">
        <p role="status" className="text-sm">
          Your password is changed. Every device was signed out.
        </p>
        <Link href="/login" className="text-sm text-primary hover:underline">
          Sign in
        </Link>
      </CardContent>
    );
  }

  return (
    <form onSubmit={(e) => void handleSubmit(e)}>
      <CardContent className="space-y-4">
        {error && (
          <div role="alert" className="rounded-md bg-destructive/15 p-3 text-sm text-destructive">
            {error}
          </div>
        )}
        <div className="space-y-2">
          <Label htmlFor="new-password">New password</Label>
          <Input
            id="new-password"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            disabled={isLoading}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="confirm-password">Confirm new password</Label>
          <Input
            id="confirm-password"
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(e) => setConfirm(e.target.value)}
            required
            disabled={isLoading}
          />
        </div>
      </CardContent>
      <CardFooter>
        <Button type="submit" className="w-full" disabled={isLoading}>
          {isLoading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          Set new password
        </Button>
      </CardFooter>
    </form>
  );
}

export default function ResetPasswordPage() {
  return (
    <div className="flex min-h-screen items-center justify-center bg-background p-4">
      <Card className="w-full max-w-md">
        <CardHeader className="space-y-1">
          <CardTitle className="text-2xl font-bold">Choose a new password</CardTitle>
          <CardDescription>
            The link works once. Setting a password signs out every device.
          </CardDescription>
        </CardHeader>
        <Suspense fallback={null}>
          <ResetPasswordForm />
        </Suspense>
      </Card>
    </div>
  );
}
