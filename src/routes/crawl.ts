import { Router } from 'express';
import { z } from 'zod';

import { prisma } from '../lib/prisma';
import { crawlCompanyWebsite } from '../services/websiteCrawler';

const paramsSchema = z.object({
  jobId: z.string().min(1)
});

const bodySchema = z.object({
  maxPagesPerCompany: z.number().int().min(1).max(10).default(4)
});

export const crawlRouter = Router();

crawlRouter.post('/:jobId/crawl', async (req, res, next) => {
  try {
    const { jobId } = paramsSchema.parse(req.params);
    const { maxPagesPerCompany } = bodySchema.parse(req.body ?? {});

    const job = await prisma.searchJob.findUnique({
      where: { id: jobId },
      include: {
        companies: {
          where: {
            websiteUrl: {
              not: null
            }
          }
        }
      }
    });

    if (!job) {
      return res.status(404).json({ message: 'Search job not found' });
    }

    await prisma.searchJob.update({
      where: { id: job.id },
      data: {
        status: 'RUNNING',
        startedAt: job.startedAt ?? new Date()
      }
    });

    let totalPages = 0;

    for (const company of job.companies) {
      if (!company.websiteUrl) {
        continue;
      }

      const pages = await crawlCompanyWebsite({
        websiteUrl: company.websiteUrl,
        maxPages: maxPagesPerCompany
      });

      if (pages.length > 0) {
        await prisma.crawlPage.createMany({
          data: pages.map((page) => ({
            companyId: company.id,
            url: page.url,
            title: page.title,
            statusCode: page.statusCode,
            crawledAt: page.crawledAt
          })),
          skipDuplicates: true
        });

        await prisma.auditLog.create({
          data: {
            searchJobId: job.id,
            event: 'COMPANY_CRAWLED',
            details: {
              companyId: company.id,
              websiteUrl: company.websiteUrl,
              pagesCrawled: pages.length
            }
          }
        });
      }

      totalPages += pages.length;
    }

    await prisma.searchJob.update({
      where: { id: job.id },
      data: {
        status: 'COMPLETED',
        finishedAt: new Date()
      }
    });

    return res.status(200).json({
      jobId: job.id,
      companiesCrawled: job.companies.length,
      pagesCrawled: totalPages
    });
  } catch (error) {
    return next(error);
  }
});
