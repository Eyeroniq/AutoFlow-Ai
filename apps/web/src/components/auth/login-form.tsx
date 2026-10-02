"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useForm } from "react-hook-form";

import { ErrorAlert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { FormField } from "@/components/ui/form-field";
import { useRedirectIfAuthenticated } from "@/hooks/use-auth";
import { api } from "@/lib/api";
import { safeNext } from "@/lib/route-guard";
import { tokenStorage } from "@/lib/token-storage";
import { type LoginValues, loginSchema } from "@/lib/validation";

export function LoginForm() {
  const router = useRouter();
  const [serverError, setServerError] = useState<string | null>(null);
  useRedirectIfAuthenticated();

  const {
    register,
    handleSubmit,
    formState: { errors, isSubmitting },
  } = useForm<LoginValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: { email: "", password: "" },
  });

  const onSubmit = handleSubmit(async (values) => {
    setServerError(null);
    try {
      const tokens = await api.auth.login(values);
      tokenStorage.setTokens(tokens);
      router.replace(safeNext(new URLSearchParams(window.location.search).get("next")) ?? "/dashboard");
    } catch (error) {
      setServerError(error instanceof Error ? error.message : "Login failed");
    }
  });

  return (
    <form onSubmit={onSubmit} noValidate className="space-y-5">
      {serverError && <ErrorAlert message={serverError} />}
      <FormField
        label="Email"
        type="email"
        autoComplete="email"
        placeholder="you@company.com"
        registration={register("email")}
        error={errors.email}
      />
      <FormField
        label="Password"
        type="password"
        autoComplete="current-password"
        registration={register("password")}
        error={errors.password}
      />
      <Button type="submit" loading={isSubmitting} className="w-full">
        {isSubmitting ? "Signing in…" : "Sign in"}
      </Button>
    </form>
  );
}
