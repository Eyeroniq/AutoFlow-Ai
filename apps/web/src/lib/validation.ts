import { z } from "zod";

// Keep these limits in sync with apps/api/app/schemas/auth.py.
const PASSWORD_MIN = 8;
const PASSWORD_MAX_BYTES = 72;

const email = z.string().trim().toLowerCase().min(1, "Email is required").pipe(z.email("Enter a valid email address"));

export const loginSchema = z.object({
  email,
  password: z.string().min(1, "Password is required"),
});

export const registerSchema = z
  .object({
    full_name: z.string().trim().min(1, "Full name is required").max(255, "Full name is too long"),
    email,
    password: z
      .string()
      .min(PASSWORD_MIN, `Password must be at least ${PASSWORD_MIN} characters`)
      .refine(
        (value) => new TextEncoder().encode(value).length <= PASSWORD_MAX_BYTES,
        `Password must be at most ${PASSWORD_MAX_BYTES} bytes`,
      ),
    confirm_password: z.string().min(1, "Please confirm your password"),
  })
  .refine((data) => data.password === data.confirm_password, {
    error: "Passwords don't match",
    path: ["confirm_password"],
  });

export type LoginValues = z.infer<typeof loginSchema>;
export type RegisterValues = z.infer<typeof registerSchema>;
