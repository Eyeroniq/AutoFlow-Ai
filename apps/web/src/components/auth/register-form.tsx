"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useForm } from "react-hook-form";

import { ErrorAlert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { FormField } from "@/components/ui/form-field";
import { useRedirectIfAuthenticated } from "@/hooks/use-auth";
import { ApiError, api } from "@/lib/api";
import { safeNext } from "@/lib/route-guard";
import { tokenStorage } from "@/lib/token-storage";
import { type RegisterValues, registerSchema } from "@/lib/validation";

export function RegisterForm() {
  const router = useRouter();
  const [serverError, setServerError] = useState<string | null>(null);
  useRedirectIfAuthenticated();

  const {
    register,
    handleSubmit,
    setError,
    formState: { errors, isSubmitting },
  } = useForm<RegisterValues>({
    resolver: zodResolver(registerSchema),
    defaultValues: { full_name: "", email: "", password: "", confirm_password: "" },
  });

  const onSubmit = handleSubmit(async ({ full_name, email, password }) => {
    setServerError(null);
    try {
      const tokens = await api.auth.register({ full_name, email, password });
      tokenStorage.setTokens(tokens);
      router.replace(safeNext(new URLSearchParams(window.location.search).get("next")) ?? "/dashboard");
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setError("email", { message: "An account with this email already exists" });
        return;
      }
      setServerError(error instanceof Error ? error.message : "Registration failed");
    }
  });

  return (
    <form onSubmit={onSubmit} noValidate className="space-y-5">
      {serverError && <ErrorAlert message={serverError} />}
      <FormField
        label="Full name"
        autoComplete="name"
        placeholder="Ada Lovelace"
        registration={register("full_name")}
        error={errors.full_name}
      />
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
        autoComplete="new-password"
        placeholder="At least 8 characters"
        registration={register("password")}
        error={errors.password}
      />
      <FormField
        label="Confirm password"
        type="password"
        autoComplete="new-password"
        registration={register("confirm_password")}
        error={errors.confirm_password}
      />
      <Button type="submit" loading={isSubmitting} className="w-full">
        {isSubmitting ? "Creating account…" : "Create account"}
      </Button>
    </form>
  );
}
