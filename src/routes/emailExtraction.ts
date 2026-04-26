import { Prisma } from '@prisma/client';
import { Router } from 'express';
import { z } from 'zod';

import { prisma } from '../lib/prisma';
import { extractEmailsFromUrl } from '../services/emailExtractor';

const paramsSchema = z.object({
  jobId: z.string().min(1)
});

export const emailExtractionRouter = Router();

emailExtractionRouter.post('/:jobId/extract-emails', async (req, res, next) => {
  try {
    const { jobId } = paramsSchema.parse(req.params);

    const job = await prisma.searchJob.findUnique({
      where: { id: jobId },
      include: {
        companies: {
          include: {
            crawlPages: true
          }
        }
      }
    });

    if (!job) {
      return res.status(404).json({ message: 'Search job not found' });
    }

    const suppressionList = await prisma.suppressionList.findMany({
      select: {
        email: true
      }
    });

    const suppressed = new Set(suppressionList.map((item) => item.email.toLowerCase()));

    let insertedContacts = 0;

    for (const company of job.companies) {
      const sourceUrls = Array.from(new Set(company.crawlPages.map((page) => page.url)));
      const uniqueByEmail = new Map<string, Prisma.ContactCreateManyInput>();

      for (const sourceUrl of sourceUrls) {
        const extracted = await extractEmailsFromUrl(sourceUrl);

        for (const record of extracted) {
          if (suppressed.has(record.email)) {
            continue;
          }

          if (!uniqueByEmail.has(record.email)) {
            uniqueByEmail.set(record.email, {
              companyId: company.id,
              email: record.email,
              sourceUrl: record.sourceUrl,
              verification: 'UNVERIFIED'
            });
          }
        }
      }

      if (uniqueByEmail.size > 0) {
        const created = await prisma.contact.createMany({
          data: Array.from(uniqueByEmail.values()),
          skipDuplicates: true
        });

        insertedContacts += created.count;
      }
    }

    await prisma.auditLog.create({
      data: {
        searchJobId: job.id,
        event: 'EMAILS_EXTRACTED',
        details: {
          insertedContacts,
          companiesProcessed: job.companies.length
        }
      }
    });

    return res.status(200).json({
      jobId: job.id,
      insertedContacts,
      companiesProcessed: job.companies.length
    });
  } catch (error) {
    return next(error);
  }
});
