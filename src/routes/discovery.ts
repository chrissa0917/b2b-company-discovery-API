import { Router } from 'express';
import { Prisma } from '@prisma/client';
import { z } from 'zod';

import { env } from '../config/env';
import { prisma } from '../lib/prisma';
import { buildCompanyDiscoveryService } from '../services/companyDiscovery';

const paramsSchema = z.object({
  jobId: z.string().min(1)
});

export const discoveryRouter = Router();

const discoveryService = buildCompanyDiscoveryService(
  env.DISCOVERY_PROVIDER,
  env.TAVILY_API_KEY,
  env.SERPAPI_API_KEY
);

discoveryRouter.post('/:jobId/discover', async (req, res, next) => {
  try {
    const { jobId } = paramsSchema.parse(req.params);

    const job = await prisma.searchJob.findUnique({
      where: { id: jobId }
    });

    if (!job) {
      return res.status(404).json({ message: 'Search job not found' });
    }

    const discoveredCompanies = await discoveryService.discover({
      keyword: job.keyword,
      location: job.location,
      limit: job.limit
    });

    const created = await prisma.$transaction(async (tx) => {
      await tx.searchJob.update({
        where: { id: job.id },
        data: {
          status: 'RUNNING',
          startedAt: job.startedAt ?? new Date()
        }
      });

      const inserts: Prisma.CompanyCreateManyInput[] = discoveredCompanies.map((company) => ({
        searchJobId: job.id,
        name: company.name,
        websiteUrl: company.websiteUrl
      }));

      if (inserts.length > 0) {
        await tx.company.createMany({
          data: inserts,
          skipDuplicates: false
        });
      }

      await tx.auditLog.createMany({
        data: discoveredCompanies.map((company) => ({
          searchJobId: job.id,
          event: 'COMPANY_DISCOVERED',
          details: {
            name: company.name,
            websiteUrl: company.websiteUrl,
            sourceUrl: company.sourceUrl
          }
        }))
      });

      await tx.searchJob.update({
        where: { id: job.id },
        data: {
          status: 'COMPLETED',
          finishedAt: new Date()
        }
      });

      return inserts.length;
    });

    return res.status(200).json({
      jobId: job.id,
      discoveredCount: discoveredCompanies.length,
      createdCompanies: created
    });
  } catch (error) {
    return next(error);
  }
});
