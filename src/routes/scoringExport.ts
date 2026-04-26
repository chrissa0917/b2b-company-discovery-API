import { Router } from 'express';
import { z } from 'zod';

import { prisma } from '../lib/prisma';
import { isCompliantBusinessContact } from '../services/complianceFilter';
import { scoreContact } from '../services/contactScoring';
import { CsvContactRow, exportContactsToCsv } from '../services/csvExport';

const paramsSchema = z.object({
  jobId: z.string().min(1)
});

export const scoringExportRouter = Router();

scoringExportRouter.post('/:jobId/score-export', async (req, res, next) => {
  try {
    const { jobId } = paramsSchema.parse(req.params);

    const job = await prisma.searchJob.findUnique({
      where: { id: jobId },
      include: {
        companies: {
          include: {
            contacts: true
          }
        }
      }
    });

    if (!job) {
      return res.status(404).json({ message: 'Search job not found' });
    }

    const suppressions = await prisma.suppressionList.findMany({
      select: { email: true }
    });

    const suppressedEmails = new Set(suppressions.map((item) => item.email.toLowerCase()));

    const csvRows: CsvContactRow[] = [];
    let compliantCount = 0;
    let filteredCount = 0;

    for (const company of job.companies) {
      let companyScoreTotal = 0;
      let companyScoreCount = 0;

      for (const contact of company.contacts) {
        const compliant = isCompliantBusinessContact({
          email: contact.email,
          sourceUrl: contact.sourceUrl,
          suppressedEmails
        });

        if (!compliant) {
          filteredCount += 1;

          await prisma.contact.update({
            where: { id: contact.id },
            data: {
              confidenceScore: 0,
              verification: 'INVALID'
            }
          });

          continue;
        }

        const score = scoreContact({
          email: contact.email,
          sourceUrl: contact.sourceUrl,
          companyWebsiteUrl: company.websiteUrl
        });

        await prisma.contact.update({
          where: { id: contact.id },
          data: {
            confidenceScore: score
          }
        });

        compliantCount += 1;
        companyScoreTotal += score;
        companyScoreCount += 1;

        csvRows.push({
          companyName: company.name,
          companyWebsiteUrl: company.websiteUrl,
          email: contact.email,
          sourceUrl: contact.sourceUrl,
          score
        });
      }

      const leadScore = companyScoreCount === 0 ? 0 : Math.round(companyScoreTotal / companyScoreCount);

      await prisma.company.update({
        where: { id: company.id },
        data: {
          leadScore
        }
      });
    }

    const csvPath = await exportContactsToCsv(job.id, csvRows);

    await prisma.auditLog.create({
      data: {
        searchJobId: job.id,
        event: 'CONTACTS_SCORED_AND_EXPORTED',
        details: {
          compliantCount,
          filteredCount,
          csvPath
        }
      }
    });

    return res.status(200).json({
      jobId: job.id,
      compliantContacts: compliantCount,
      filteredContacts: filteredCount,
      csvPath
    });
  } catch (error) {
    return next(error);
  }
});
