import { Router } from 'express';
import { z } from 'zod';

import { prisma } from '../lib/prisma';

const searchPayloadSchema = z.object({
  keyword: z.string().trim().min(1),
  location: z.string().trim().min(1),
  limit: z.number().int().positive().max(100).default(25)
});

export const searchRouter = Router();

searchRouter.post('/', async (req, res, next) => {
  try {
    const payload = searchPayloadSchema.parse(req.body);

    const job = await prisma.searchJob.create({
      data: {
        keyword: payload.keyword,
        location: payload.location,
        limit: payload.limit
      },
      select: {
        id: true
      }
    });

    return res.status(201).json({ jobId: job.id });
  } catch (error) {
    return next(error);
  }
});
