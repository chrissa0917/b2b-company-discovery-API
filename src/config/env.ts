import dotenv from 'dotenv';
import { z } from 'zod';

dotenv.config();

const envSchema = z.object({
  NODE_ENV: z.enum(['development', 'test', 'production']).default('development'),
  PORT: z.coerce.number().int().positive().default(3000),
  DATABASE_URL: z.string().min(1),
  DISCOVERY_PROVIDER: z.enum(['mock', 'tavily', 'serpapi']).default('mock'),
  TAVILY_API_KEY: z.string().min(1).optional(),
  SERPAPI_API_KEY: z.string().min(1).optional()
});

export const env = envSchema.parse(process.env);
