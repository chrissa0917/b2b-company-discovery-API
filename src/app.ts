import express from 'express';
import { ZodError } from 'zod';

import { crawlRouter } from './routes/crawl';
import { discoveryRouter } from './routes/discovery';
import { emailExtractionRouter } from './routes/emailExtraction';
import { scoringExportRouter } from './routes/scoringExport';
import { searchRouter } from './routes/search';

export const app = express();

app.use(express.json());

app.get('/health', (_req, res) => {
  res.status(200).json({ status: 'ok' });
});

app.use('/api/search', searchRouter);
app.use('/api/search', discoveryRouter);
app.use('/api/search', crawlRouter);
app.use('/api/search', emailExtractionRouter);
app.use('/api/search', scoringExportRouter);

app.use((error: unknown, _req: express.Request, res: express.Response, _next: express.NextFunction) => {
  if (error instanceof ZodError) {
    return res.status(400).json({
      message: 'Invalid request payload',
      errors: error.flatten()
    });
  }

  console.error(error);
  return res.status(500).json({ message: 'Internal server error' });
});
