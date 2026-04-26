import { mkdir } from 'node:fs/promises';
import path from 'node:path';

import { createObjectCsvWriter } from 'csv-writer';

export interface CsvContactRow {
  companyName: string;
  companyWebsiteUrl: string | null;
  email: string;
  sourceUrl: string;
  score: number;
}

export async function exportContactsToCsv(jobId: string, rows: CsvContactRow[]): Promise<string> {
  const exportDir = path.resolve(process.cwd(), 'exports');
  await mkdir(exportDir, { recursive: true });

  const outputPath = path.join(exportDir, `job-${jobId}-contacts.csv`);

  const writer = createObjectCsvWriter({
    path: outputPath,
    header: [
      { id: 'companyName', title: 'company_name' },
      { id: 'companyWebsiteUrl', title: 'company_website_url' },
      { id: 'email', title: 'email' },
      { id: 'sourceUrl', title: 'source_url' },
      { id: 'score', title: 'score' }
    ]
  });

  await writer.writeRecords(rows);

  return outputPath;
}
